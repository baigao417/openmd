"""Gated OpenCLI wrapper for WeChat group history discovery.

The provider only runs when registered through ``--enable-browser-backends``.
Tests use fake runners. Openmd does not read WeChat databases, extract DB keys,
decrypt local stores, open accounts, or persist raw command output here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

from openmd.core.opencli_output import load_opencli_json
from openmd.adapters.high_privacy import HIGH_PRIVACY_STATUS_KEY, high_privacy_status_metadata
from openmd.adapters.wechat_group.adapter import parse_wechat_group_selector, render_wechat_group_markdown
from openmd.backends.base import DiscoveryRequest, DiscoveryResult
from openmd.core.models import ContentItem
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import run_interruptibly


OPENCLI_WX_HISTORY_DISCOVERY_BACKEND = "opencli.wx.history"
DEFAULT_WX_HISTORY_TIMEOUT_SECONDS = 180
CommandRunner = Callable[[list[str], int], tuple[int, str, str]]


class OpenCliWechatGroupHistoryProvider:
    """Parse sanitized WeChat group history JSON from an explicitly enabled CLI."""

    name = OPENCLI_WX_HISTORY_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_WX_HISTORY_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        try:
            selector = parse_wechat_group_selector(request.input_url)
        except ValueError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="target_scope_required",
                error_summary=sanitize_error_summary(exc),
                metadata={"platform": request.platform, "limit": request.limit},
            )

        command = build_opencli_wx_history_args(
            str(selector["group_name"]),
            since=selector.get("since"),
            until=selector.get("until"),
            limit=request.limit,
        )
        exit_code, stdout, stderr = self._runner(command, self.timeout)
        if exit_code != 0:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=_exit_error_code(exit_code),
                error_summary=sanitize_error_summary(stderr or stdout) or "opencli wx history failed.",
                source_notes=["OpenCLI stdout/stderr was not persisted by Openmd."],
                metadata={"platform": request.platform, "limit": request.limit},
            )

        try:
            messages = parse_opencli_wx_history_output(stdout)
        except ValueError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_discovery_output",
                error_summary=sanitize_error_summary(exc),
                source_notes=["OpenCLI stdout was invalid JSON and was not persisted."],
                metadata={"platform": request.platform, "limit": request.limit},
            )
        if request.limit is not None and not request.allow_all:
            messages = messages[: max(0, request.limit)]
        if not messages:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="no_delta",
                error_summary="opencli wx history returned no scoped group messages.",
                source_notes=["OpenCLI JSON was parsed in memory and was not persisted."],
                metadata={
                    "platform": request.platform,
                    "limit": request.limit,
                    HIGH_PRIVACY_STATUS_KEY: high_privacy_status_metadata(
                        platform=request.platform,
                        target_kind="wechat_group",
                        state="no_delta",
                        next_action=(
                            "If messages are expected, refresh the scoped local WeChat cache/export and rerun the same selector."
                        ),
                        permission_scope="user_accessible",
                    ),
                },
            )

        item = _conversation_item(
            group_name=str(selector["group_name"]),
            input_url=request.input_url,
            messages=messages,
            since=selector.get("since"),
            until=selector.get("until"),
            limit=request.limit,
        )
        return DiscoveryResult.success(
            provider=self.name,
            items=[item],
            source_notes=[
                "OpenCLI wx history JSON was parsed in memory.",
                "No WeChat database, key extraction, decryption, login, or broad session scan was attempted by Openmd.",
            ],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "normalized_url": request.input_url,
            },
        )


def build_opencli_wx_history_args(
    group_name: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[str]:
    command = ["opencli", "wx", "history", group_name, "--json"]
    if since:
        command.extend(["--since", since])
    if until:
        command.extend(["--until", until])
    if limit is not None:
        command.extend(["--limit", str(max(0, limit))])
    return command


def parse_opencli_wx_history_output(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = load_opencli_json(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("opencli wx history did not return valid JSON") from exc

    messages: list[dict[str, Any]] = []
    for index, row in enumerate(_candidate_rows(payload), start=1):
        if not isinstance(row, dict):
            continue
        message = _parse_message_row(row, index=index)
        if message is not None:
            messages.append(message)
    return messages


def _candidate_rows(payload: object) -> Iterable[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return ()
    for key in ("messages", "items", "list", "data", "records", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = list(_candidate_rows(value))
            if nested:
                return nested
    return ()


def _parse_message_row(row: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    text = _first_string(row, "text", "content", "message", "msg")
    speaker = _first_string(row, "speaker", "sender", "from", "author", "nickname", "name")
    timestamp = _first_string(row, "timestamp", "time", "datetime", "date", "created_at")
    if not text and not row.get("attachments"):
        return None
    return {
        "id": _first_string(row, "id", "msg_id", "message_id") or f"msg_{index:04d}",
        "timestamp": timestamp or "unknown-time",
        "speaker": speaker or "unknown",
        "text": text or "",
        "attachments": _list_or_empty(row.get("attachments")),
        "links": _list_or_empty(row.get("links")),
        "source_map": [],
    }


def _conversation_item(
    *,
    group_name: str,
    input_url: str,
    messages: list[dict[str, Any]],
    since: str | None,
    until: str | None,
    limit: int | None,
) -> ContentItem:
    title = f"WeChat group: {group_name}"
    return ContentItem(
        platform="wechat_group",
        input_type="collection",
        url=input_url,
        id=f"wxg-{_stable_hash(group_name, since, until)}",
        title=title,
        source_type="conversation",
        raw_meta={
            "profile": "conversation",
            "permission_scope": "user_accessible",
            "group_name": group_name,
            "since": since,
            "until": until,
            "messages": messages,
            "markdown_preview": render_wechat_group_markdown(messages),
            "discovery_limit": limit,
            "discovery_provider": OPENCLI_WX_HISTORY_DISCOVERY_BACKEND,
        },
    )


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    resolved = shutil.which(command[0]) or command[0]
    try:
        completed = run_interruptibly(
            [resolved, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, "", "opencli executable is not available on PATH."
    except subprocess.TimeoutExpired:
        return 124, "", f"opencli wx history timed out after {timeout}s."
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _first_string(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _stable_hash(*parts: str | None) -> str:
    import hashlib

    return hashlib.sha256(":".join(part or "" for part in parts).encode("utf-8")).hexdigest()[:12]


def _exit_error_code(exit_code: int) -> str:
    if exit_code == 127:
        return "discovery_tool_missing"
    if exit_code == 124:
        return "discovery_timeout"
    return f"discovery_exit_{exit_code}"
