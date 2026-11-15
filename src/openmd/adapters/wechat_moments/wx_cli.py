"""Gated OpenCLI wrapper for WeChat Moments person-feed discovery."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

from openmd.core.opencli_output import load_opencli_json
from openmd.adapters.high_privacy import HIGH_PRIVACY_STATUS_KEY, high_privacy_status_metadata
from openmd.adapters.wechat_moments.adapter import (
    parse_wechat_moments_selector,
    render_wechat_moments_markdown,
)
from openmd.backends.base import DiscoveryRequest, DiscoveryResult
from openmd.core.models import ContentItem
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import run_interruptibly


OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND = "opencli.wx.sns-feed"
DEFAULT_WX_SNS_TIMEOUT_SECONDS = 180
CommandRunner = Callable[[list[str], int], tuple[int, str, str]]


class OpenCliWechatMomentsProvider:
    """Parse sanitized Moments JSON from an explicitly enabled local CLI."""

    name = OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_WX_SNS_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        try:
            selector = parse_wechat_moments_selector(request.input_url)
        except ValueError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="target_scope_required",
                error_summary=sanitize_error_summary(exc),
                metadata={"platform": request.platform, "limit": request.limit},
            )

        command = build_opencli_wx_sns_feed_args(
            str(selector["person"]),
            since=selector.get("since"),
            until=selector.get("until"),
            limit=request.limit,
        )
        exit_code, stdout, stderr = self._runner(command, self.timeout)
        if exit_code != 0:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=_exit_error_code(exit_code),
                error_summary=sanitize_error_summary(stderr or stdout) or "opencli wx sns-feed failed.",
                source_notes=["OpenCLI stdout/stderr was not persisted by Openmd."],
                metadata={"platform": request.platform, "limit": request.limit},
            )

        try:
            posts = parse_opencli_wx_sns_feed_output(stdout)
        except ValueError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_discovery_output",
                error_summary=sanitize_error_summary(exc),
                source_notes=["OpenCLI stdout was invalid JSON and was not persisted."],
                metadata={"platform": request.platform, "limit": request.limit},
            )
        if request.limit is not None and not request.allow_all:
            posts = posts[: max(0, request.limit)]
        if not posts:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="no_delta",
                error_summary="opencli wx sns-feed returned no scoped Moments posts.",
                source_notes=["OpenCLI JSON was parsed in memory and was not persisted."],
                metadata={
                    "platform": request.platform,
                    "limit": request.limit,
                    HIGH_PRIVACY_STATUS_KEY: high_privacy_status_metadata(
                        platform=request.platform,
                        target_kind="wechat_moments_person",
                        state="no_delta",
                        next_action=(
                            "If posts are expected, refresh the scoped local WeChat cache/export and rerun the same selector."
                        ),
                        permission_scope="user_accessible",
                    ),
                },
            )

        item = _feed_item(
            person=str(selector["person"]),
            input_url=request.input_url,
            posts=posts,
            since=selector.get("since"),
            until=selector.get("until"),
            limit=request.limit,
        )
        return DiscoveryResult.success(
            provider=self.name,
            items=[item],
            source_notes=[
                "OpenCLI wx sns-feed JSON was parsed in memory.",
                "No broad contacts scan, database read, key extraction, decryption, login, or account mutation was attempted by Openmd.",
            ],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "normalized_url": request.input_url,
            },
        )


def build_opencli_wx_sns_feed_args(
    person: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[str]:
    command = ["opencli", "wx", "sns-feed", "--user", person, "--json"]
    if since:
        command.extend(["--since", since])
    if until:
        command.extend(["--until", until])
    if limit is not None:
        command.extend(["--limit", str(max(0, limit))])
    return command


def parse_opencli_wx_sns_feed_output(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = load_opencli_json(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("opencli wx sns-feed did not return valid JSON") from exc

    posts: list[dict[str, Any]] = []
    for index, row in enumerate(_candidate_rows(payload), start=1):
        if not isinstance(row, dict):
            continue
        post = _parse_post_row(row, index=index)
        if post is not None:
            posts.append(post)
    return posts


def _candidate_rows(payload: object) -> Iterable[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return ()
    for key in ("moments", "posts", "items", "list", "data", "records", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = list(_candidate_rows(value))
            if nested:
                return nested
    return ()


def _parse_post_row(row: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    text = _first_string(row, "text", "content", "description", "message")
    speaker = _first_string(row, "speaker", "author", "user", "nickname", "name")
    timestamp = _first_string(row, "timestamp", "time", "datetime", "date", "created_at")
    attachments = _list_or_empty(row.get("attachments") or row.get("media") or row.get("images"))
    if not text and not attachments:
        return None
    return {
        "id": _first_string(row, "id", "moment_id", "sns_id") or f"moment_{index:04d}",
        "timestamp": timestamp or "unknown-time",
        "speaker": speaker or "unknown",
        "text": text or "",
        "attachments": attachments,
        "links": _list_or_empty(row.get("links")),
        "source_map": [],
    }


def _feed_item(
    *,
    person: str,
    input_url: str,
    posts: list[dict[str, Any]],
    since: str | None,
    until: str | None,
    limit: int | None,
) -> ContentItem:
    return ContentItem(
        platform="wechat_moments",
        input_type="collection",
        url=input_url,
        id=f"wxm-{_stable_hash(person, since, until)}",
        title=f"WeChat Moments: {person}",
        source_type="conversation",
        raw_meta={
            "profile": "conversation",
            "permission_scope": "user_accessible",
            "person": person,
            "since": since,
            "until": until,
            "messages": posts,
            "markdown_preview": render_wechat_moments_markdown(posts),
            "discovery_limit": limit,
            "discovery_provider": OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND,
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
        return 124, "", f"opencli wx sns-feed timed out after {timeout}s."
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
    return hashlib.sha256(":".join(part or "" for part in parts).encode("utf-8")).hexdigest()[:12]


def _exit_error_code(exit_code: int) -> str:
    if exit_code == 127:
        return "discovery_tool_missing"
    if exit_code == 124:
        return "discovery_timeout"
    return f"discovery_exit_{exit_code}"
