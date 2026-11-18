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
from openmd.core.chat_title import conversation_title
from openmd.core.login import normalize_login_result
from openmd.core.opencli_output import load_opencli_json
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import run_interruptibly


CHATGPT_BACKEND = "opencli.chatgpt.detail"
DEEPSEEK_BACKEND = "opencli.deepseek.detail"
GROK_BACKEND = "opencli.grok.detail"
PLANNED_AI_CHAT_BACKENDS = (
    "opencli.claude.detail",
)
IMPLEMENTED_AI_CHAT_SITES = frozenset({"chatgpt", "deepseek", "grok", "doubao", "gemini", "qwen", "yuanbao"})
EXPERIMENTAL_AI_CHAT_SITES = frozenset({"doubao", "gemini", "qwen", "yuanbao"})

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
                registry.register(backend, OpenCliAiChatProvider(site, backend) if site in IMPLEMENTED_AI_CHAT_SITES else UnimplementedAiChatProvider(site, backend))


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
    if site == "chatgpt":
        command.extend(["--wait", "true", "--stable", "2", "--timeout", "60"])
    if site in {"chatgpt", "grok", "qwen"}:
        command.extend(["--markdown", "true"])
    return command


def parse_ai_chat_output(payload: Any, *, site: str, source_url: str, include_thinking: bool = False) -> tuple[list[TurnBlock], dict[str, Any]]:
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
    if len(rows) == 1 and isinstance(rows[0], dict) and str(rows[0].get("Role") or rows[0].get("role") or "").lower() == "system" and str(rows[0].get("Text") or rows[0].get("text") or "") in {"No messages found. Verify the conversation ID.", "No visible messages found."}:
        raise AiChatError("ai_chat_empty", "平台返回了空会话，未取得任何轮次。")
    if any(_is_generating(row) for row in rows):
        raise AiChatError("ai_chat_generating", "对话仍在生成中，请等待生成完成后重试。")
    turns: list[TurnBlock] = []
    thinking_count = 0
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
        thinking = _first_text(row, "thinking", "Thinking", "reasoning_content", "reasoning", "thoughts", strip=False)
        if str(role).lower() == "assistant" and isinstance(thinking, str) and thinking.strip():
            thinking_count += 1
            if include_thinking:
                text = "#### 思考过程\n\n" + thinking.strip() + "\n\n#### 最终回答\n\n" + str(text)
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
        "thinking_omitted_count": 0 if include_thinking else thinking_count,
        "thinking_included_count": thinking_count if include_thinking else 0,
    }
    title_sources = [value for value in (payload, root) if isinstance(value, dict)]
    page_title = next((value[key] for value in title_sources for key in ("title", "Title", "page_title") if isinstance(value.get(key), str) and value[key].strip()), None)
    session_title = next((value[key] for value in title_sources for key in ("conversation_title", "ConversationTitle") if isinstance(value.get(key), str) and value[key].strip()), None)
    meta["title"] = conversation_title(site, page_title, turns, session_title=session_title)
    independent_counts = [row.get("OpenmdPageTurnCount") for row in rows if isinstance(row, dict) and type(row.get("OpenmdPageTurnCount")) is int and row["OpenmdPageTurnCount"] >= 0]
    if independent_counts:
        meta["independent_turn_count"] = max(independent_counts)
    return turns, meta


class AiChatError(ValueError):
    def __init__(self, code: str, summary: str, *, original_code: str | None = None):
        super().__init__(summary)
        self.code = code
        self.original_code = original_code or code


def login_status_from_output(payload: Any) -> bool | None:
    """Use status fields, never conversation prose, to identify login recovery."""
    if isinstance(payload, str):
        try:
            payload = load_opencli_json(payload)
        except (ValueError, TypeError):
            return None
    if _find_login_url(payload):
        return False
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and _is_auth_error_code(str(error.get("code", "")).upper()):
            return False
        payload = payload.get("data", payload)
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            login, state = str(row.get("Login") or "").lower(), str(row.get("Status") or "").lower()
            if login == "yes" and state != "page not ready":
                return True
            if state == "login required" or (login.startswith("no") and state == "connected"):
                return False
    return None


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
        if self.runner is _run_command and self.site in {"chatgpt", "doubao", "gemini", "qwen", "yuanbao"}:
            code, status_stdout, _ = self.runner(["opencli", self.site, "status", "-f", "json"], min(self.timeout, 90))
            if code == 77 or login_status_from_output(status_stdout) is False:
                return normalize_login_result(_failed(self.name, "login_required", "平台当前未登录，未导出对话。"), item)
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
                    if self.runner is _run_command and attempt < 1:
                        continue
                    return _failed(self.name, "ai_chat_empty", "平台返回了空会话，未取得任何轮次。")
                if error_code in {"TIMEOUT", "AI_CHAT_NOT_STABLE", "SESSION_BUSY"}:
                    failure = _failed(self.name, "ai_chat_timeout" if error_code == "TIMEOUT" else "ai_chat_not_stable", "平台对话未在有限等待内稳定，请稍后重试。")
                    failure.metadata["original_error_code"] = error_code
                    return failure
                return _failed(self.name, f"backend_exit_{code}", "OpenCLI conversation detail command failed.")
            try:
                turns, metadata = parse_ai_chat_output(stdout, site=self.site, source_url=item.url, include_thinking=item.raw_meta.get("include_thinking") is True)
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
            title=conversation_title(self.site, item.title, turns, session_title=metadata["title"]),
            metadata=metadata,
            blocks=turns,
            source_notes=["OpenCLI detail returned conversation turns; no summary or turn merging was performed.",
                          *([f"已省略思考过程：{metadata['thinking_omitted_count']} 段。"] if metadata["thinking_omitted_count"] else []),
                          *([f"已保留思考过程：{metadata['thinking_included_count']} 段。"] if metadata["thinking_included_count"] else []),
                          "detail 仅按独立思考字段识别；未分离的思考文字保持原样，待真实 DOM 验证。"],
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
    for key in ("generating", "Generating", "is_generating", "in_progress", "streaming"):
        if value.get(key) is True or str(value.get(key, "")).lower() in {"true", "yes"}:
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
        kwargs = {}
        if len(command) > 2 and command[1] in IMPLEMENTED_AI_CHAT_SITES and command[2] in {"detail", "read", "status", "history"}:
            from openmd.runtime.ai_chat_loading import detail_loading_environment

            kwargs["env"] = _grok_loading_environment() if command[1:3] == ["grok", "detail"] else detail_loading_environment(command[1], command[2])
        completed = run_interruptibly(command, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8", errors="replace", **kwargs)
    except FileNotFoundError:
        return 127, "", "opencli executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "opencli conversation detail timed out"
    return completed.returncode, completed.stdout or "", completed.stderr or ""
