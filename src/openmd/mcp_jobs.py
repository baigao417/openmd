"""MCP adapter for the existing serial job queue and single-save CLI service."""
from __future__ import annotations

import json
import math
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# Isolated worker processes use this installed file, including in editable worktrees.
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openmd import browser_host as host
from openmd.core.secrets import is_sensitive_query_key, SENSITIVE_TEXT_PATTERNS

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 32_000
MAX_BATCH = 20
GATES = {"network": "--enable-network-backends", "browser": "--enable-browser-backends",
         "media": "--enable-media-download", "asr": "--enable-asr", "cloud": "--enable-cloud-backends"}
RECEIPT_FIELDS = ("status", "source_level", "completeness", "sync", "login", "next_action", "error_code")


@contextmanager
def queue():
    # A browser runner from another installed version must never consume MCP jobs.
    token = host.JOB_NAMESPACE.set("mcp-jobs")
    try:
        yield
    finally:
        host.JOB_NAMESPACE.reset(token)


def _spawn():
    # The runner executes this installed file (also inside editable worktrees);
    # the hidden Windows launch itself stays in browser_host._spawn_runner.
    host._spawn_runner(command=[sys.executable, "-I", str(Path(__file__).resolve()), "--runner"])


def destination(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError("output_directory_must_be_absolute")
    return Path(value).resolve()


def receipt(result: dict) -> dict:
    def label(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_.:-]{1,100}", value) else None

    def structure(value, depth=0):
        if depth > 5:
            return None
        if isinstance(value, dict):
            allowed = {"status", "checks", "signal", "reason", "gap", "operation", "required", "state",
                       "complete", "expected_count", "actual_count", "missing_count", "exported_count", "stats_count",
                       "missing_pages", "output_pages", "unverified_pages", "total_pages", "start_seconds", "end_seconds", "limit"}
            return {key: structure(child, depth + 1) for key, child in value.items() if key in allowed}
        if isinstance(value, list):
            return [structure(child, depth + 1) for child in value[:20]]
        if type(value) in (bool, int) or (type(value) is float and math.isfinite(value)):
            return value
        return label(value)

    code = label(result.get("error_code"))
    status = result.get("status") if result.get("status") in {"verified", "partial", "failed"} else None
    action = None
    if code == "login_required":
        action = "Sign in using the source application, then submit a fresh save."
    elif code or status == "failed":
        action = "Check local readiness with openmd_doctor and the CLI doctor, then submit a fresh save."
    elif status == "partial":
        action = "Inspect completeness and the saved artifact before using this source."
    login = result.get("login")
    login_summary = {"required": bool(login.get("required", True))} if isinstance(login, dict) and login else None
    if code == "login_required":
        login_summary = {"required": True}
    return {"status": status, "source_level": label(result.get("source_level")),
            "completeness": structure(result.get("completeness")), "sync": structure(result.get("sync")),
            "login": login_summary, "next_action": action, "error_code": code}


def _validate_source_url(value: str) -> str:
    url = host._validate_url(value)
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or any(
        is_sensitive_query_key(key) for key, _ in parse_qsl(parsed.query) + parse_qsl(parsed.fragment)
    ):
        raise ValueError("credential_url_not_allowed")
    return url


def _initial(status: dict) -> dict:
    result = {"id": status["id"], "state": status["state"], **receipt({})}
    if status.get("batch"):
        result["batch_id"] = status["batch"]
    return result


def _validate_source(source: str) -> str:
    if not isinstance(source, str) or not source or len(source) > 4096:
        raise ValueError("invalid_source")
    if source.startswith(("http://", "https://")):
        return _validate_source_url(source)
    if not Path(source).is_absolute() or not Path(source).is_file():
        raise ValueError("source_must_be_http_url_or_absolute_file")
    return source


def _save_one(source: str, target: Path, *, batch_id: str | None = None) -> dict:
    source = _validate_source(source)
    job_id = uuid.uuid4().hex
    job_dir = host.jobs_root() / job_id
    job_dir.mkdir(parents=True)
    status = {"id": job_id, "state": "queued", "url": source, "save_to": str(target),
              "created": time.time(), "seq": host._next_seq(), "attempts": 0, "mcp": True,
              "permissions": {name: False for name in GATES}}
    if batch_id:
        status["batch"] = batch_id
    host._write_status(job_dir, status)
    return status


def save(source: str, output_directory: str, *, network: bool = False, browser: bool = False,
         media: bool = False, asr: bool = False, cloud: bool = False) -> dict:
    target = destination(output_directory)
    with queue():
        status = _save_one(source, target)
        status["permissions"] = dict(network=network, browser=browser, media=media, asr=asr, cloud=cloud)
        host._write_status(host.jobs_root() / status["id"], status)
        _spawn()
    return _initial(status)


def save_batch(sources: list[str], output_directory: str, *, network: bool = False, browser: bool = False,
               media: bool = False, asr: bool = False, cloud: bool = False) -> dict:
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources_required")
    if len(sources) > MAX_BATCH:
        raise ValueError("too_many_sources")
    target = destination(output_directory)
    # Validate every item before writing any status file, so one bad item cannot
    # leave a half-created batch behind.
    normalized = [_validate_source(source) for source in sources]
    batch_id = uuid.uuid4().hex
    permissions = dict(network=network, browser=browser, media=media, asr=asr, cloud=cloud)
    with queue():
        statuses = []
        for source in normalized:
            status = _save_one(source, target, batch_id=batch_id)
            status["permissions"] = permissions.copy()
            host._write_status(host.jobs_root() / status["id"], status)
            statuses.append(status)
        _spawn()
    return {"batch_id": batch_id, "jobs": [_initial(status) for status in statuses], "count": len(statuses)}


def save_page(source_url: str, output_directory: str, *, html: str | None = None,
              html_path: str | None = None) -> dict:
    target = destination(output_directory)
    source_url = _validate_source_url(source_url)
    if (html is None) == (html_path is None):
        raise ValueError("provide_exactly_one_of_html_or_html_path")
    if html_path is not None:
        path = Path(html_path)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("html_path_must_be_absolute_file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_HTML_BYTES + 1)
        if len(raw) > MAX_HTML_BYTES:
            raise ValueError("snapshot_too_large")
        html = raw.decode("utf-8-sig")
    if not isinstance(html, str) or not html:
        raise ValueError("empty_snapshot_html")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("snapshot_too_large")
    with queue():
        status = host.start_snapshot_job(html, source_url, save_to=str(target), spawn=lambda *a, **k: None)
        status.update(mcp=True, permissions={name: False for name in GATES})
        host._write_status(host.jobs_root() / status["id"], status)
        _spawn()
    return _initial(status)


def command(status: dict) -> list[str]:
    return [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--execute", status["id"]]


def arguments(status: dict):
    from openmd.cli import build_parser

    source = status["snapshot_path"] if status.get("snapshot") else status["url"]
    argv = ["go", source, "--save-to", status["save_to"], "--json"]
    if status.get("snapshot"):
        argv += ["--as-snapshot", "--url", status["url"]]
    argv += [flag for name, flag in GATES.items() if status["permissions"].get(name) is True]
    args = build_parser().parse_args(argv)
    args._explicit_permissions = True
    return args


def _job_status(job_id: str, *, include_content: bool = False) -> dict:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("invalid_job_id")
    with queue():
        status = host.read_status(job_id)
    fields = dict(status.get("receipt", {}))
    for key in ("login", "error_code"):
        if key in status:
            fields[key] = status[key]
    result = {"id": job_id, "state": status["state"], **receipt(fields),
              "saved_to": status.get("saved_to")}
    if status["state"] == "unknown":
        result.update(error_code="job_not_found", next_action="Check the job ID and the server data directory.")
    if status["state"] == "failed" and not result["error_code"]:
        result.update(status="failed", error_code="extraction_failed",
                      next_action="Check local readiness with openmd_doctor and the CLI doctor, then submit a fresh save.")
    if include_content and status.get("saved_to"):
        path = Path(status["saved_to"]).resolve()
        root = Path(status["save_to"]).resolve()
        if path.is_file() and path.is_relative_to(root) and path.suffix.lower() == ".md":
            with path.open(encoding="utf-8") as stream:
                content = stream.read(MAX_CONTENT_CHARS + 1)
            bounded = content[:MAX_CONTENT_CHARS]
            redacted = bounded
            for pattern in SENSITIVE_TEXT_PATTERNS:
                redacted = pattern.sub("[redacted]", redacted)
            result.update(content=redacted[:MAX_CONTENT_CHARS], content_truncated=max(len(content), len(redacted)) > MAX_CONTENT_CHARS,
                          content_redacted=redacted != bounded)
    return result


def job(job_id: str, *, action: str = "status", include_content: bool = False) -> dict:
    if action not in {"status", "cancel"}:
        raise ValueError("invalid_job_action")
    if action == "cancel":
        with queue():
            host.cancel_job(job_id)
    return _job_status(job_id, include_content=include_content)


def resource_index() -> str:
    items = []
    with queue():
        root = host.jobs_root()
        if root.is_dir():
            for path in sorted(root.glob("*/status.json"), key=lambda item: item.stat().st_mtime, reverse=True):
                try:
                    status = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not status.get("mcp"):
                    continue
                saved = status.get("saved_to")
                if status.get("state") != "done" or not saved:
                    continue
                artifact = Path(saved).resolve()
                destination_root = Path(status.get("save_to", "")).resolve()
                if not artifact.is_file() or not artifact.is_relative_to(destination_root):
                    continue
                items.append({"uri": f"openmd://job/{status['id']}", "name": status["id"],
                              "mimeType": "text/markdown", "description": "Openmd MCP saved Markdown artifact"})
                if len(items) >= MAX_BATCH:
                    break
    return json.dumps(items, ensure_ascii=False)


def resource_content(job_id: str) -> str:
    result = job(job_id, include_content=True)
    content = result.get("content")
    if isinstance(content, str):
        return content
    return json.dumps({key: value for key, value in result.items() if key != "saved_to"}, ensure_ascii=False)


def doctor() -> dict:
    from openmd.core.doctor import build_doctor_report

    report = build_doctor_report("core")
    return {"profile": "core", "ready": bool(report.get("ready")),
            "manifest_valid": bool(report.get("manifest_valid")),
            "network_access": "not_probed", "private_session_state": "not_read",
            "next_action": "Use openmd_save or openmd_save_page." if report.get("ready") else "Run the CLI doctor for optional dependency details."}


def main() -> int:
    with queue():
        if sys.argv[1:] == ["--runner"]:
            host.run_queue()
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "--execute":
            from openmd.interactive import cmd_go
            from openmd.runtime.managed_tools import activate
            from openmd.runtime.services import finish_openmd_bridge

            status = host.read_status(sys.argv[2])
            try:
                if status["permissions"].get("browser") is True:
                    activate()
                return cmd_go(arguments(status))
            finally:
                finish_openmd_bridge()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
