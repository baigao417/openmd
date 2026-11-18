"""OpenCLI backed AI conversation export providers.

The parser is intentionally tolerant of OpenCLI 1.x bare JSON and 2.x
envelopes. It only projects role/text/time/message id fields into turn blocks;
raw browser payloads are never persisted.
"""

from __future__ import annotations

import re
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
from openmd.core.login import normalize_login_result
from openmd.core.opencli_output import load_opencli_json
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import run_interruptibly


CHATGPT_BACKEND = "opencli.chatgpt.detail"
DEEPSEEK_BACKEND = "opencli.deepseek.detail"
GROK_BACKEND = "opencli.grok.detail"
PLANNED_AI_CHAT_BACKENDS = (
    "opencli.claude.detail",
    "opencli.doubao.detail",
    "opencli.gemini.detail",
    "opencli.qwen.detail",
    "opencli.yuanbao.detail",
)

AI_CHAT_BACKENDS = {
    "chatgpt": CHATGPT_BACKEND,
    "deepseek": DEEPSEEK_BACKEND,
    "grok": GROK_BACKEND,
    "claude": "opencli.claude.detail",
    "doubao": "opencli.doubao.detail",
    "gemini": "opencli.gemini.detail",
    "qwen": "opencli.qwen.detail",
    "yuanbao": "opencli.yuanbao.detail",
}

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    requested = options.backend_override
    if options.enable_browser_backends:
        if requested in {None, CHATGPT_BACKEND}:
            registry.register(CHATGPT_BACKEND, OpenCliAiChatProvider("chatgpt", CHATGPT_BACKEND))
        if requested in {None, DEEPSEEK_BACKEND}:
            registry.register(DEEPSEEK_BACKEND, OpenCliAiChatProvider("deepseek", DEEPSEEK_BACKEND))
        if requested in {None, GROK_BACKEND}:
            registry.register(GROK_BACKEND, OpenCliAiChatProvider("grok", GROK_BACKEND))
        for site, backend in AI_CHAT_BACKENDS.items():
            if site in {"chatgpt", "deepseek", "grok"}:
                continue
            if requested in {None, backend}:
                registry.register(backend, UnimplementedAiChatProvider(site, backend))


def ai_chat_backend_for_site(site: str) -> str | None:
    return AI_CHAT_BACKENDS.get(site)


def conversation_id_for_url(site: str, value: str) -> str | None:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    path = parsed.path.rstrip("/")
    if site == "chatgpt":
        match = re.search(r"/(?:c|share)/([^/]+)$", path)
    elif site == "claude":
        match = re.search(r"/chat/([^/]+)$", path)
    elif site == "deepseek":
        match = re.search(r"/a/chat/s/([^/]+)$", path)
    elif site == "doubao":
        match = re.search(r"/(?:chat|conversation)/([^/]+)$", path)
    elif site == "gemini":
        match = re.search(r"/(?:app|u/\d+/app)/([^/]+)$", path)
    elif site == "qwen":
        match = re.search(r"/chat/([^/]+)$", path)
    elif site == "yuanbao":
        match = re.search(r"/chat/([^/]+)/([^/]+)$", path)
    elif site == "grok":
        match = re.search(r"/(?:c|chat)/([^/]+)$", path)
    else:
        match = None
    if not match:
        return None
    if site == "yuanbao":
        return f"{match.group(1)}/{match.group(2)}"
    return match.group(1)


def build_opencli_detail_args(site: str, conversation_id: str) -> list[str]:
    command = ["opencli", site, "detail", conversation_id, "-f", "json"]
    if site in {"chatgpt", "grok"}:
        command.extend(["--markdown", "true"])
    return command


def parse_ai_chat_output(payload: Any, *, site: str, source_url: str) -> tuple[list[TurnBlock], dict[str, Any]]:
    """Parse a fixture or OpenCLI payload into verbatim turns."""
    if isinstance(payload, str):
        payload = load_opencli_json(payload)
    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error")
        code = str(error.get("code") or "").upper() if isinstance(error, dict) else ""
        detail = error.get("message") if isinstance(error, dict) else str(error or "")
        if _is_auth_error_code(code) or (not code and _looks_like_auth_error(detail)):
            raise AiChatError("ai_chat_not_logged_in", "平台当前未登录，未导出对话。", original_code=code or "ai_chat_not_logged_in")
        if code == "EMPTY_RESULT":
            raise AiChatError("ai_chat_empty", "平台返回了空会话，未取得任何轮次。")
        raise AiChatError("ai_chat_detail_failed", "平台对话详情读取失败。")
    if _find_login_url(payload):
        raise AiChatError("login_required", "平台当前位于登录页，未导出对话。", original_code="login_required")
    root = payload
    if isinstance(root, dict):
        for key in ("conversation", "chat", "data", "result"):
            candidate = root.get(key)
            if isinstance(candidate, (dict, list)):
                root = candidate
                break
    if _is_generating(root):
        raise AiChatError("ai_chat_generating", "对话仍在生成中，请等待生成完成后重试。")
    rows = _find_message_rows(root)
    turns: list[TurnBlock] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        role = _first_text(row, "role", "Role", "speaker", "author", "sender") or "unknown"
        text = _first_text(row, "text", "Text", "content", "message", "markdown", "body", strip=False)
        if isinstance(row.get("content"), dict):
            text = _first_text(row["content"], "text", "parts", "value", strip=False) or text
        if isinstance(text, list):
            text = "\n".join(str(part) for part in text if part is not None)
        if not text or not str(text).strip():
            continue
        turns.append(TurnBlock(
            role=str(role),
            text=str(text),
            time=_first_text(row, "time", "timestamp", "created_at", "createdAt", "Timestamp"),
            message_id=_first_text(row, "message_id", "messageId", "id", "Id"),
        ))
    if not turns:
        raise AiChatError("ai_chat_empty", "平台返回了空会话，未取得任何轮次。")
    meta = {
        "chat_site": site,
        "coverage": "provider_declared_complete" if _declares_complete(payload) else "visible_branch",
        "turn_count": len(turns),
        "assistant_turn_count": sum(1 for turn in turns if str(turn.role).lower() == "assistant"),
        "source_url": source_url,
    }
    return turns, meta


class AiChatError(ValueError):
    def __init__(self, code: str, summary: str, *, original_code: str | None = None):
        super().__init__(summary)
        self.code = code
        self.original_code = original_code or code


class OpenCliAiChatProvider:
    def __init__(self, site: str, name: str, *, runner: CommandRunner | None = None, timeout: int = 180) -> None:
        self.site, self.name = site, name
        self.runner = runner or _run_command
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session export",
            capabilities=(BackendCapability(
                platforms=("ai_chat",), source_types=("conversation",), input_types=("single",),
                source_levels=("browser_rendered", "manual_export", "partial_extraction"),
                requires_network=True, requires_credentials=True,
            ),),
            description=f"Export {self.site} conversation turns through OpenCLI.",
            provider=self.name,
        )

    def health(self) -> BackendHealth:
        if self.runner is not _run_command:
            return BackendHealth(ok=True, dependencies={"opencli": "injected"})
        if not (shutil.which("opencli") or shutil.which("opencli.cmd")):
            return BackendHealth(ok=False, dependencies={"opencli": "missing"}, error_code="backend_dependency_missing", error_summary="opencli executable is not available on PATH.")
        return BackendHealth(ok=True, dependencies={"opencli": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "ai_chat":
            return _failed(self.name, "unsupported_platform", "AI chat provider only supports ai_chat inputs.")
        site = str(item.raw_meta.get("chat_site") or "")
        if site != self.site:
            return _failed(self.name, "ai_chat_site_mismatch", "The selected provider does not match this conversation URL.")
        conversation_id = conversation_id_for_url(self.site, item.url)
        if not conversation_id:
            return _failed(self.name, "ai_chat_invalid_url", "Conversation URL did not contain a valid conversation ID.")
        command = build_opencli_detail_args(self.site, conversation_id)
        turns = None
        metadata = None
        for attempt in range(3):
            try:
                code, stdout, stderr = self.runner(command, self.timeout)
            except Exception as exc:
                return _failed(self.name, "ai_chat_command_failed", sanitize_error_summary(exc) or "OpenCLI detail command failed.")
            if code:
                structured = _parse_error_payload(stdout)
                error = structured.get("error") if isinstance(structured, dict) else None
                error_code = str(error.get("code") or "").upper() if isinstance(error, dict) else ""
                error_message = str(error.get("message") or "") if isinstance(error, dict) else ""
                if code == 77 or _is_auth_error_code(error_code) or (not error_code and (_looks_like_auth_error(error_message) or _looks_like_auth_error(stderr))):
                    result = _failed(self.name, error_code or ("backend_exit_77" if code == 77 else "ai_chat_not_logged_in"), "平台当前未登录，未导出对话。")
                    return normalize_login_result(result, item)
                if error_code == "EMPTY_RESULT":
                    return _failed(self.name, "ai_chat_empty", "平台返回了空会话，未取得任何轮次。")
                if error_code in {"TIMEOUT", "AI_CHAT_NOT_STABLE", "SESSION_BUSY"}:
                    failure = _failed(self.name, "ai_chat_timeout" if error_code == "TIMEOUT" else "ai_chat_not_stable", "平台对话未在有限等待内稳定，请稍后重试。")
                    failure.metadata["original_error_code"] = error_code
                    return failure
                return _failed(self.name, f"backend_exit_{code}", "OpenCLI conversation detail command failed.")
            try:
                turns, metadata = parse_ai_chat_output(stdout, site=self.site, source_url=item.url)
            except AiChatError as exc:
                result = _failed(self.name, exc.code, str(exc))
                result.metadata["original_error_code"] = exc.original_code
                return normalize_login_result(result, item)
            assistant_count = int((metadata or {}).get("assistant_turn_count") or 0)
            stability_check = self.runner is _run_command
            if self.site not in {"chatgpt", "deepseek"} or assistant_count > 0 or attempt == 2:
                break
            if stability_check:
                time.sleep(0.4)
        if turns is None or metadata is None:
            return _failed(self.name, "ai_chat_not_stable", "平台对话详情在有限等待后仍未稳定。")
        if self.runner is _run_command and self.site in {"chatgpt", "deepseek"} and not int(metadata.get("assistant_turn_count") or 0):
            return _failed(self.name, "ai_chat_not_stable", "平台助手轮次未在有限等待后出现。")
        if self.site == "grok":
            metadata["source_url"] = f"https://grok.com/c/{conversation_id}"
            anchor = item.raw_meta.get("anchor_response_id") or parse_qs(urlparse(item.url).query).get("rid", [None])[0]
            if anchor:
                metadata["anchor_response_id"] = anchor
        return TranscriptResult(
            text="\n\n".join(turn.text for turn in turns) + "\n",
            status="verified",
            source_level="browser_rendered",
            backend=self.name,
            title=item.title or f"{self.site} conversation {conversation_id}",
            metadata=metadata,
            blocks=turns,
            source_notes=["OpenCLI detail returned conversation turns; no summary or turn merging was performed."],
        )


class UnimplementedAiChatProvider:
    def __init__(self, site: str, name: str) -> None:
        self.site, self.name = site, name

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(name=self.name, layer="planned browser/session export", capabilities=(), description=f"{self.site} AI chat export is declared but not implemented.", provider=self.name)

    def health(self) -> BackendHealth:
        return BackendHealth(ok=False, error_code="ai_chat_site_not_implemented", error_summary=f"{self.site} AI chat export is not implemented.")

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        return _failed(self.name, "ai_chat_site_not_implemented", f"AI chat site not implemented: {self.site}.")


def _find_message_rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        if any(isinstance(row, dict) and any(key in row for key in ("role", "Role", "speaker", "content", "text", "Text", "message")) for row in value):
            return value
        for child in value:
            found = _find_message_rows(child)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("messages", "turns", "items", "messages_data", "history"):
            if key in value:
                found = _find_message_rows(value[key])
                if found:
                    return found
        for child in value.values():
            found = _find_message_rows(child)
            if found:
                return found
    return []


def _first_text(value: dict[str, Any], *keys: str, strip: bool = True) -> str | list[Any] | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, (str, int, float)) and str(candidate).strip():
            return str(candidate).strip() if strip else str(candidate)
        if isinstance(candidate, list) and candidate:
            return candidate
    return None


def _is_generating(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("generating", "is_generating", "in_progress", "streaming"):
        if value.get(key) is True:
            return True
    state = str(value.get("status") or value.get("state") or "").lower()
    return state in {"generating", "streaming", "in_progress", "pending"}


def _find_login_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and (key.lower() in {"url", "href", "location", "current_url"} or "url" in key.lower()):
                parsed = urlparse(child)
                if "/auth/login" in parsed.path.lower() or parsed.path.lower().rstrip("/") in {"/login", "/signin", "/sign-in"}:
                    return child
            found = _find_login_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_login_url(child)
            if found:
                return found
    return None


def _declares_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("complete") is True or value.get("coverage") in {"complete", "full"} or value.get("is_complete") is True


def _is_auth_error_code(code: str) -> bool:
    return code in {"AUTH_REQUIRED", "LOGIN_WALL", "NOT_LOGGED_IN", "SESSION_EXPIRED"}


def _looks_like_auth_error(value: str) -> bool:
    return bool(re.search(r"(?:未登录|登录|\blog(?:in|ged[ -]?in)\b|\bauth(?:entication|orization)?\b|\banonymous\b|\bunauthorized\b|\bsign[ -]?in\b)", value or "", re.IGNORECASE))


def _parse_error_payload(value: str) -> dict[str, Any] | None:
    try:
        parsed = load_opencli_json(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else None


def _failed(backend: str, code: str, summary: str) -> TranscriptResult:
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=backend, error_code=code, error_summary=summary, source_notes=["AI conversation provider returned a structured failure."])


def _grok_loading_environment() -> dict[str, str]:
    from openmd.runtime.ai_chat_loading import detail_loading_environment

    return detail_loading_environment("grok")


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        kwargs = {"env": _grok_loading_environment()} if command[1:3] == ["grok", "detail"] else {}
        completed = run_interruptibly(command, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8", errors="replace", **kwargs)
    except FileNotFoundError:
        return 127, "", "opencli executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "opencli conversation detail timed out"
    return completed.returncode, completed.stdout or "", completed.stderr or ""
