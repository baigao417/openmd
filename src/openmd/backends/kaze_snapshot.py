"""Read a rendered page through Kaze's public BrowserMCP interface."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult
from openmd.runtime.tempdirs import owned_temporary_directory

KAZE_BACKEND = "browser.kaze.snapshot"
PROTOCOL_VERSION = "2024-11-05"
INITIALIZE_TIMEOUT_S = 3.0
TOOLS_TIMEOUT_S = 2.0
TOTAL_TIMEOUT_S = 10.0
PAGE_READY_TIMEOUT_MS = 20_000
READ_TIMEOUT_MS = 15_000
MAX_HTML_BYTES = 50 * 1024 * 1024
REQUIRED_TOOLS = {
    "tabs_list": {"scope", "limit"},
    "claim_tab": {"url", "expectedUrl"},
    "open_tab": {"url"},
    "release_tab": {"tabId"},
    "close_tab": {"tabId"},
    "session_finish": set(),
    "js": {"code", "timeoutMs", "maxChars"},
}


class KazeError(RuntimeError):
    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(summary)
        self.code, self.next_action = code, next_action


@dataclass(frozen=True)
class PageSnapshot:
    html: str
    url: str
    title: str


@dataclass(frozen=True)
class KazeCapability:
    installed: bool
    launcher: str | None
    available: bool
    protocol_version: str | None = None
    kaze_version: str | None = None
    bridge_version: str | None = None
    server_version: str | None = None
    schema_fingerprint: str | None = None
    error_code: str | None = None
    next_action: str | None = None
    disabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "launcher_available": bool(self.installed and self.launcher),
            "available": self.available,
            "protocol_version": self.protocol_version,
            "kaze_version": self.kaze_version,
            "bridge_version": self.bridge_version,
            "server_version": self.server_version,
            "schema_fingerprint": self.schema_fingerprint,
            "disabled": self.disabled,
            "error_code": self.error_code,
            "next_action": self.next_action,
            "private_session_state": "not_read",
        }


def _launcher() -> Path:
    override = os.environ.get("KAZE_MCP_LAUNCHER")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Kite" / "bin" / "kaze-mcp.cmd"


def _error_from_exception(exc: BaseException) -> KazeError:
    message = str(exc).lower()
    if "external agents" in message or "doesn't allow other agents" in message:
        return KazeError("kaze_external_agents_disabled", "Kaze does not allow external agents.", "Enable External Agents in Kaze settings, then retry.")
    if "process exited" in message or "process closed" in message:
        return KazeError("kaze_not_running", "Kaze's BrowserMCP process exited before replying.", "Start Kaze and allow External Agents in Kaze settings, then retry.")
    if isinstance(exc, TimeoutError):
        return KazeError("kaze_timeout", "Kaze did not respond before the bounded timeout.", "Check that Kaze is open and External Agents are enabled, then retry.")
    return KazeError("kaze_unavailable", "Kaze MCP could not be reached.", "Start Kaze and allow External Agents in Kaze settings, then retry.")


def _schema_fingerprint(tools: list[dict[str, Any]]) -> str:
    import hashlib

    signatures = []
    for tool in sorted(tools, key=lambda row: str(row.get("name", ""))):
        name = tool.get("name")
        if name in REQUIRED_TOOLS:
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            signatures.append((name, sorted((schema.get("properties") or {}).keys()), sorted(schema.get("required") or [])))
    return hashlib.sha256(json.dumps(signatures, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _verify_tools(tools: list[dict[str, Any]]) -> str:
    by_name = {tool.get("name"): tool for tool in tools if isinstance(tool, dict)}
    for name, properties in REQUIRED_TOOLS.items():
        tool = by_name.get(name)
        if tool is None:
            raise KazeError("kaze_unsupported_protocol", "Kaze MCP is missing a required BrowserMCP tool.", "Update Kaze and retry after its BrowserMCP tools are available.")
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        exposed = set((schema.get("properties") or {}).keys())
        if not properties.issubset(exposed):
            raise KazeError("kaze_tool_schema_changed", "A required BrowserMCP tool has an unsupported argument schema.", "Update Kaze and retry; Openmd will not guess changed tool arguments.")
    return _schema_fingerprint(tools)


class _McpSession:
    def __init__(self, launcher: Path) -> None:
        self.process = subprocess.Popen(
            ["cmd.exe", "/d", "/s", "/c", str(launcher)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.errors: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, args=(self.process.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._read, args=(self.process.stderr, self.errors), daemon=True).start()
        self.sequence = 0
        self.initialized: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.fingerprint: str | None = None

    @staticmethod
    def _read(stream, sink) -> None:
        try:
            for line in stream:
                sink.put(line)
        finally:
            sink.put(None)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = TOTAL_TIMEOUT_S) -> dict[str, Any]:
        self.sequence += 1
        request_id = self.sequence
        request = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        if not self.process.stdin:
            raise RuntimeError("Kaze MCP input is unavailable")
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"{method} timed out") from exc
            if line is None:
                stderr = "".join(row or "" for row in list(self.errors.queue))[-500:]
                raise RuntimeError(stderr or "Kaze MCP process exited")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError("Kaze MCP request failed")
                return message

    def notify(self, method: str) -> None:
        if self.process.stdin:
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
            self.process.stdin.flush()

    def handshake(self) -> None:
        initialize = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "openmd", "version": "0.3.1"},
        }, INITIALIZE_TIMEOUT_S)
        result = initialize.get("result") or {}
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise KazeError("kaze_unsupported_protocol", "Kaze MCP negotiated an unsupported protocol version.", "Update Kaze or use another Openmd route.")
        self.initialized = result
        self.notify("notifications/initialized")
        listed = self.request("tools/list", {}, TOOLS_TIMEOUT_S)
        self.tools = list((listed.get("result") or {}).get("tools") or [])
        self.fingerprint = _verify_tools(self.tools)

    def call(self, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        result = response.get("result") or {}
        if result.get("isError"):
            text = " ".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
            raise RuntimeError(text[:300])
        return _tool_payload(result)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def _tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if structured.get("ok") is True and isinstance(structured.get("value"), dict):
            return structured["value"]
        return structured
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                value = json.loads(item["text"])
                if isinstance(value, dict):
                    if value.get("ok") is True and isinstance(value.get("value"), dict):
                        return value["value"]
                    return value
            except (TypeError, ValueError):
                pass
    return result


def _open_session() -> _McpSession:
    launcher = _launcher()
    if not launcher.is_file():
        raise KazeError("kaze_launcher_missing", "Kaze MCP launcher is not installed.", "Install or repair Kaze, then retry.")
    session = _McpSession(launcher)
    try:
        session.handshake()
        return session
    except Exception:
        session.close()
        raise


def probe_kaze(*, disabled: bool | None = None, session_factory=None) -> KazeCapability:
    launcher = _launcher()
    is_disabled = (os.environ.get("OPENMD_KAZE") == "0") if disabled is None else disabled
    if is_disabled:
        return KazeCapability(launcher.is_file(), str(launcher) if launcher.is_file() else None, False,
                              error_code="kaze_disabled", next_action="Set OPENMD_KAZE=1 to enable Kaze routing.", disabled=True)
    if not launcher.is_file() and session_factory is None:
        return KazeCapability(False, str(launcher), False, error_code="kaze_launcher_missing",
                              next_action="Install or repair Kaze, then rerun openmd doctor.")
    session = None
    try:
        session = (session_factory or _open_session)()
        if not session.fingerprint:
            session.handshake()
        server = session.initialized.get("serverInfo") or {}
        try:
            session.call("session_finish", {"keep": []}, timeout=1)
        except Exception:
            pass
        return KazeCapability(True, str(launcher), True,
                              protocol_version=session.initialized.get("protocolVersion"),
                              server_version=server.get("version"), schema_fingerprint=session.fingerprint)
    except KazeError as exc:
        return KazeCapability(launcher.is_file(), str(launcher), False, error_code=exc.code, next_action=exc.next_action)
    except Exception as exc:
        error = _error_from_exception(exc)
        return KazeCapability(launcher.is_file(), str(launcher), False, error_code=error.code, next_action=error.next_action)
    finally:
        if session is not None:
            session.close()


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    if options.backend_override not in {None, KAZE_BACKEND} or not options.enable_browser_backends:
        return
    if os.environ.get("OPENMD_KAZE") == "0":
        return
    registry.register(KAZE_BACKEND, KazeSnapshotProvider())


class KazeSnapshotProvider:
    name = KAZE_BACKEND

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="browser_rendered",
            capabilities=(BackendCapability(
                platforms=("web_article", "x"), source_types=("article",), input_types=("single",),
                source_levels=("browser_rendered", "third_party_extractor", "partial_extraction"),
                requires_network=False, requires_credentials=True,
            ),),
            description="Capture the already-rendered page through Kaze's public BrowserMCP interface.",
            provider="openmd.backends.kaze_snapshot",
        )

    def health(self) -> BackendHealth:
        capability = probe_kaze()
        return BackendHealth(ok=capability.available, error_code=capability.error_code,
                             error_summary=capability.next_action,
                             dependencies={"kaze_mcp": capability.server_version or "unavailable"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        session = None
        owned_tab_id = None
        borrowed_tab_id = None
        result: TranscriptResult | None = None
        try:
            session = _open_session()
            url = _normalize_url(request.item.url)
            rows = session.call("tabs_list", {"scope": "unclaimed", "limit": 100}, timeout=5)
            candidates = rows.get("tabs", rows.get("items", []))
            matching = next((row for row in candidates if isinstance(row, dict) and _same_url(str(row.get("url", "")), url)), None)
            if matching:
                claimed = session.call("claim_tab", {"url": url, "expectedUrl": url}, timeout=5)
                borrowed_tab_id = str(claimed.get("tabId") or claimed.get("tab") or matching.get("tabId") or "") or None
                tab_id = borrowed_tab_id
            else:
                opened = session.call("open_tab", {"url": url}, timeout=5)
                tab_id = str(opened.get("tabId") or opened.get("tab") or opened.get("id") or "") or None
                owned_tab_id = tab_id
            if not tab_id:
                raise KazeError("kaze_tab_open_failed", "Kaze did not return a page tab identifier.", "Check Kaze and retry the URL.")
            ready_js = (
                "const t = await browser.tabs.get(" + json.dumps(tab_id) + "); "
                "await t.expect({target:{selector:'body'},visible:true},{timeoutMs:" + str(PAGE_READY_TIMEOUT_MS) + "}); "
                "({ready: await t.evaluate(() => document.readyState)})"
            )
            try:
                session.call("js", {"code": ready_js, "timeoutMs": PAGE_READY_TIMEOUT_MS,
                                    "maxChars": 1024}, timeout=PAGE_READY_TIMEOUT_MS / 1000 + 1)
            except Exception as exc:
                if not isinstance(exc, TimeoutError) and "timeout" not in str(exc).lower():
                    raise
                raise KazeError("kaze_page_not_ready", "The requested page did not finish rendering in Kaze.",
                                "Wait for the page to finish loading in Kaze, then retry.") from exc
            from openmd.backends.loaded_chat import snapshot_item
            from openmd.backends.snapshot import SnapshotProvider

            with owned_temporary_directory(prefix="openmd-kaze-snapshot-") as directory:
                path = Path(directory) / "snapshot.html"
                js = (
                    "const t = await browser.tabs.get(" + json.dumps(tab_id) + "); "
                    "const html = await t.evaluate(() => document.documentElement.outerHTML); "
                    "const url = await t.url(); const title = await t.evaluate(() => document.title); "
                    "if (Buffer.byteLength(html, 'utf8') > " + str(MAX_HTML_BYTES) + ") throw new Error('snapshot_too_large'); "
                    "const fs = await import('node:fs/promises'); "
                    "await fs.writeFile(" + json.dumps(str(path)) + ", html, 'utf8'); "
                    "({url, title, bytes: Buffer.byteLength(html, 'utf8')})"
                )
                page = session.call("js", {"code": js, "timeoutMs": READ_TIMEOUT_MS, "maxChars": 4096}, timeout=READ_TIMEOUT_MS / 1000 + 1)
                final_url = page.get("url")
                title = page.get("title")
                try:
                    html = path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise KazeError("kaze_snapshot_transfer_failed", "Kaze did not write the rendered page snapshot.", "Check that Openmd's temporary directory is accessible to Kaze, then retry.") from exc
                actual_bytes = path.stat().st_size
                reported_bytes = page.get("bytes")
                if isinstance(reported_bytes, int) and reported_bytes != actual_bytes:
                    raise KazeError("kaze_snapshot_transfer_failed", "Kaze returned an incomplete page snapshot.", "Retry after checking the Kaze page and temporary storage.")
                if not re.search(r"<(?:html|body)\b", html[:4096], re.I):
                    raise KazeError("kaze_page_not_ready", "Kaze returned no rendered HTML document.", "Wait for the page to finish loading in Kaze, then retry.")
                if actual_bytes > MAX_HTML_BYTES:
                    raise KazeError("kaze_snapshot_too_large", "The rendered page exceeds Openmd's snapshot limit.", "Use the page's own export or save a smaller page section.")
                if not isinstance(final_url, str) or not _same_origin(final_url, url):
                    raise KazeError("kaze_page_url_mismatch", "Kaze ended on a different page than requested.", "Open the requested URL in Kaze and retry; login and error pages are not saved as success.")
                if _is_login_redirect(final_url):
                    raise KazeError("kaze_login_required", "Kaze was redirected to a login page.", "Sign in to the requested site in Kaze, then retry.")
                item = snapshot_item(path, url)
                result = SnapshotProvider().extract(BackendExtractRequest(item, request.backend_chain))
            metadata = {key: value for key, value in result.metadata.items() if key != "snapshot_path"}
            metadata.update({"captured_from": "browser_snapshot", "browser_backend": self.name, "coverage": "visible_only"})
            if result.status == "failed":
                code = "kaze_login_required" if result.error_code in {"login_wall", "login_required"} else result.error_code or "kaze_snapshot_failed"
                action = "Sign in to the requested site in Kaze, then retry." if code == "kaze_login_required" else "Inspect the source page in Kaze and retry."
                result = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=self.name,
                                          title=title or result.title, error_code=code, error_summary=action,
                                          source_notes=["Kaze did not provide a validated page snapshot."], metadata=metadata)
            else:
                result = TranscriptResult(text=result.text, status=result.status, source_level=result.source_level,
                                          backend=self.name, title=title or result.title, author=result.author,
                                          published_at=result.published_at, error_code=result.error_code,
                                          error_summary=result.error_summary, source_notes=result.source_notes, metadata=metadata)
        except KazeError as exc:
            result = self._failed(exc.code, str(exc), exc.next_action)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                result = self._failed("kaze_timeout", "Kaze timed out during MCP handshake or page capture.",
                                      "Check that Kaze is open and External Agents are enabled, then retry.")
            else:
                error = _error_from_exception(exc)
                result = self._failed(error.code, str(error), error.next_action)

        cleanup_errors: list[str] = []
        if session is not None:
            for tool, tab_id in (("close_tab", owned_tab_id), ("release_tab", borrowed_tab_id)):
                if not tab_id:
                    continue
                try:
                    response = session.call(tool, {"tabId": tab_id}, timeout=3)
                    if response.get("ok") is False:
                        cleanup_errors.append(tool)
                except Exception:
                    cleanup_errors.append(tool)
            try:
                finished = session.call("session_finish", {"keep": []}, timeout=3)
                failed = finished.get("failed")
                lists_present = all(isinstance(finished.get(key), list) for key in ("closed", "released", "failed"))
                if finished.get("ok") is False or not lists_present or (isinstance(failed, list) and failed):
                    cleanup_errors.append("session_finish")
            except Exception:
                cleanup_errors.append("session_finish")
            session.close()
        if result is None:
            result = self._failed("kaze_snapshot_failed", "Kaze did not produce a page result.", "Retry after checking Kaze.")
        if cleanup_errors:
            result = TranscriptResult(
                text="", status="failed", source_level="partial_extraction", backend=self.name,
                title=result.title, error_code="kaze_cleanup_failed",
                error_summary="Kaze page cleanup could not be confirmed; inspect Kaze tab state before retrying.",
                source_notes=["Kaze BrowserMCP cleanup verification failed."],
                metadata={"browser_backend": self.name, "cleanup_verified": False},
            )
        elif result.status != "failed":
            result.metadata["cleanup_verified"] = True
        return result

    def _failed(self, code: str, summary: str, next_action: str) -> TranscriptResult:
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=self.name,
                                error_code=code, error_summary=f"{summary} {next_action}",
                                source_notes=["Kaze BrowserMCP capture failed before a validated snapshot was produced."])


def _normalize_url(url: str) -> str:
    from openmd.core.input_validation import normalize_http_url

    return normalize_http_url(url)


def _same_url(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(_normalize_url(left)), urlsplit(_normalize_url(right))
        clean_a = urlunsplit((a.scheme.lower(), a.netloc.lower(), a.path or "/", a.query, ""))
        clean_b = urlunsplit((b.scheme.lower(), b.netloc.lower(), b.path or "/", b.query, ""))
        return clean_a.rstrip("/") == clean_b.rstrip("/")
    except ValueError:
        return False


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(_normalize_url(left)), urlsplit(_normalize_url(right))
        return (a.scheme.lower(), a.hostname, a.port) == (b.scheme.lower(), b.hostname, b.port)
    except ValueError:
        return False


def _is_login_redirect(url: str) -> bool:
    try:
        path = urlsplit(_normalize_url(url)).path.lower().rstrip("/")
    except ValueError:
        return False
    return any(part in path for part in ("/login", "/signin", "/sign-in", "/auth/", "/i/flow/login"))
