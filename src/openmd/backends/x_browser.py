"""Read a public X post through the user's already-open signed-in browser tab."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .youtube_discovery import _run_opencli

X_BROWSER_BACKEND = "openmd.x.logged_in_browser"
MAX_TOTAL_SECONDS = 8.0
OpenCliRunner = Callable[..., tuple[int, str, str]]


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    """Register only behind the existing explicit browser-backend gate."""
    if not options.enable_browser_backends:
        return
    if options.backend_override not in {None, X_BROWSER_BACKEND}:
        return
    registry.register(X_BROWSER_BACKEND, XLoggedInBrowserProvider())


class XLoggedInBrowserProvider:
    def __init__(self, *, runner: OpenCliRunner | None = None) -> None:
        self._runner = runner or _run_opencli

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=X_BROWSER_BACKEND,
            layer="L3 logged-in browser X API",
            capabilities=(BackendCapability(
                platforms=("x",),
                source_types=("article", "mixed"),
                input_types=("single",),
                source_levels=("browser_rendered",),
                requires_network=True,
                requires_credentials=False,
                optional_config=("browser_session",),
            ),),
            description="Read a public X post through an already-open X tab; credentials remain in the page.",
            provider="openmd.x.logged_in_browser",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={"browser": "checked on extraction"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        tweet_id = _tweet_id(item)
        if item.platform != "x" or not tweet_id:
            return _failure("invalid_input", "Pass an X status URL or numeric post ID.", "Open the public post URL and retry.")

        started = time.monotonic()
        try:
            exit_code, stdout, stderr = self._runner(
                ["opencli", "browser", "chrome", "call", "openmd.x.read", json.dumps([tweet_id])],
                timeout=MAX_TOTAL_SECONDS,
            )
        except TimeoutError:
            return _failure("x_browser_timeout", "The X page request exceeded the eight-second limit.", "Retry once; Openmd will continue to the next browser route.")
        data = _json_data(stdout) if exit_code == 0 else None
        if exit_code == 124:
            return _failure("x_browser_timeout", "The X page request exceeded the eight-second limit.", "Retry once; Openmd will continue to the next browser route.")
        result = _page_result(data)
        if not isinstance(result, dict) or result.get("ok") is not True:
            code, action = _page_failure(result, stdout, stderr)
            return _failure(code, "The signed-in X page could not return this post.", action)
        body = result.get("text")
        if not isinstance(body, str) or not body.strip():
            return _failure("x_browser_text_missing", "X returned no readable post text.", "Open the post in Chrome to confirm it is visible, then retry.")

        title = str(result.get("title") or "").strip()[:120]
        rendered = f"# {title}\n\n{body.strip()}\n" if title and title.strip() != body.strip() else body.strip() + "\n"
        elapsed = time.monotonic() - started
        if elapsed > MAX_TOTAL_SECONDS:
            return _failure("x_browser_timeout", "The signed-in X page request exceeded the eight-second limit.", "Retry once; Openmd will continue to the next browser route.")
        return TranscriptResult(
            text=rendered,
            status="verified",
            source_level="browser_rendered",
            backend=X_BROWSER_BACKEND,
            title=title or item.title or f"X-{tweet_id}",
            author=str(result.get("author") or item.author or "") or None,
            source_notes=["Read-only X page API request executed inside the already-open X page.", "Authentication material stayed in the page context."],
            metadata={"tweet_id": tweet_id, "api_layer": "logged_in_browser_x_api", "elapsed_seconds": round(elapsed, 3)},
        )


def _tweet_id(item: ContentItem) -> str | None:
    for value in (item.id, item.url):
        candidate = str(value or "")
        if candidate.isdigit():
            return candidate
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            parts = [part for part in parsed.path.split("/") if part]
            for index, part in enumerate(parts[:-1]):
                if part in {"status", "statuses"} and parts[index + 1].isdigit():
                    return parts[index + 1]
    return None


def _json_data(stdout: str) -> Any:
    try:
        envelope = json.loads(stdout.strip())
    except (TypeError, ValueError):
        envelope = None
    if envelope is not None:
        return _unwrap_data(envelope)
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return _unwrap_data(value)
    return None


def _unwrap_data(value: Any) -> Any:
    """Unwrap OpenCLI's nested daemon/command envelopes, but only a few levels."""
    for _ in range(6):
        if not isinstance(value, dict) or "data" not in value:
            break
        next_value = value["data"]
        if next_value is value:
            break
        value = next_value
    return value


def _page_result(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        return data["result"]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        value = data[0]
        if isinstance(value.get("result"), dict):
            return value["result"]
        if isinstance(value.get("data"), dict):
            return value["data"]
        return value
    return data if isinstance(data, dict) else None


def _page_failure(result: dict[str, Any] | None, stdout: str, stderr: str) -> tuple[str, str]:
    if result:
        code = str(result.get("error_code") or "x_browser_api_failed")
        action = str(result.get("next_action") or "Retry once; Openmd will continue to the next browser route.")
        return code, action
    return _bridge_error(stdout, stderr)


def _bridge_error(stdout: str, stderr: str) -> tuple[str, str]:
    error = (stdout + " " + stderr).lower()
    if "permission" in error or "scripting" in error:
        return "x_browser_permission_denied", "Reload the Openmd extension and grant its requested permissions, then retry."
    if "connect" in error or "extension" in error or "daemon" in error:
        return "x_browser_extension_unavailable", "Run openmd browser-setup and reload the extension, then retry."
    return "x_browser_bridge_failed", "Check that the Openmd extension is connected, then retry."


def _failure(code: str, summary: str, action: str) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=X_BROWSER_BACKEND,
        source_notes=["The logged-in browser X API route failed without exporting page credentials."],
        error_code=code,
        error_summary=sanitize_error_summary(f"{summary} Next step: {action}"),
        metadata={"next_action": action, "api_layer": "logged_in_browser_x_api"},
    )
