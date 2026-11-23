"""Bounded platform health checks through existing extraction/discovery routes.

Samples stay in machine-local configuration. Reports contain no URLs, source
text, response payloads, or credentials, and never enable media/ASR/cloud work.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import BackendExecutor, DiscoveryExecutor
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
)
from openmd.core.detector import PLATFORM_IDS, detect_platform
from openmd.core.local_config import checkout_root
from openmd.core.secrets import sanitize_error_summary
from openmd.routing.loader import load_catalog
from openmd.runtime.interpreters import run_interruptibly, worker_command


CONFIG_SCHEMA = "openmd.probe-config/v1"
REPORT_SCHEMA = "openmd.probe/v1"
_FORBIDDEN_GATES = frozenset({"media_download", "asr", "cloud", "local_compute", "local_export"})
# This legacy builtin only declares a browser gate, but downloads and sends
# media to Tingwu. A probe must never invoke it, even with browser permission.
_FORBIDDEN_BACKENDS = frozenset({"opencli.xiaoe.transcript"})
_BOUNDED_DISCOVERY = {
    "bilibili": {"bilibili-harvest.discovery": "network", "opencli.bilibili.user-videos": "browser"},
    "youtube": {"opencli.youtube.discovery": "browser", "opencli.youtube.playlist.discovery": "browser", "youtube.data-api.discovery": "network", "youtube.data-api.playlist.discovery": "network"},
    "douyin": {"opencli.douyin.user-videos": "browser"},
    "xiaohongshu": {"opencli.xiaohongshu.user": "browser"},
    "xiaoyuzhou": {"opencli.xiaoyuzhou.podcast-episodes": "browser"},
    "xiaoe": {"opencli.xiaoe.catalog": "browser"},
}
_URL_RE = re.compile(r"(?:https?|file)://[^\s<>\"'\])]+", re.I)


@dataclass(frozen=True)
class ProbeSample:
    url: str
    mode: str = "extract"
    discovery_backend: str | None = None


@dataclass
class ProbeResult:
    platform: str
    configured: bool
    status: str = "broken"
    mode: str = "extract"
    branch: str | None = None
    reason: str | None = None
    error_code: str | None = None
    extraction_status: str | None = None
    source_level: str | None = None
    text_chars: int = 0
    item_count: int = 0
    route_version: str | None = None
    tool_version: str | None = None
    elapsed_seconds: float = 0.0
    live_route_attempted: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    excluded_branches: list[dict[str, str]] = field(default_factory=list)
    detail_readable: bool | None = None
    transcription: str | None = None


def default_config_path() -> Path:
    explicit = os.environ.get("OPENMD_PROBE_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    root = checkout_root()
    return root / "out/probe-config.json" if root else Path.home() / ".openmd/probe-config.json"


def load_probe_samples(path: Path, *, required: bool = False) -> dict[str, ProbeSample]:
    """Read one sample per platform; reject credentials and executable options."""
    if not path.is_file():
        if required:
            raise ValueError("probe_config_missing")
        return {}
    if path.stat().st_size > 128 * 1024:
        raise ValueError("probe_config_too_large")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("probe_config_invalid_json") from exc
    if not isinstance(data, dict) or data.get("schema") != CONFIG_SCHEMA or set(data) - {"schema", "samples"}:
        raise ValueError("probe_config_invalid_schema")
    samples = data.get("samples")
    if not isinstance(samples, dict) or set(samples) - set(PLATFORM_IDS):
        raise ValueError("probe_config_invalid_platforms")
    result: dict[str, ProbeSample] = {}
    for platform, value in samples.items():
        if not isinstance(value, dict) or set(value) - {"url", "mode", "discovery_backend"}:
            raise ValueError("probe_config_invalid_sample_fields")
        url = value.get("url", "")
        mode = value.get("mode", "extract")
        backend = value.get("discovery_backend")
        if not isinstance(url, str) or mode not in {"extract", "manifest"}:
            raise ValueError("probe_config_invalid_sample")
        if backend is not None and (not isinstance(backend, str) or not backend or mode != "manifest"):
            raise ValueError("probe_config_invalid_discovery_backend")
        if url.strip():
            result[platform] = ProbeSample(url.strip(), mode, backend)
    return result


def _safe_reason(value: object, sample: str = "") -> str | None:
    if value is None:
        return None
    text = str(value)
    if sample:
        text = text.replace(sample, "[sample]")
    text = _URL_RE.sub("[url]", text)
    text = re.sub(
        r"(?i)(?:cookie|token|authorization|pass|secret|credential|api[_-]?key)"
        r"[\w-]*[\"']?\s*[:=]\s*[\"']?[^\s,;}]+",
        "[redacted]", text,
    )
    return sanitize_error_summary(text)


def _safe_chain(platform: str, chain: tuple[str, ...], options: BackendRuntimeOptions) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    catalog = load_catalog()
    safe: list[str] = []
    excluded: list[dict[str, str]] = []
    for backend in chain:
        cap = catalog.capabilities.get(backend)
        reason = None
        gates = set(cap.get("gates", [])) if cap else set()
        if backend in _FORBIDDEN_BACKENDS or gates & _FORBIDDEN_GATES:
            reason = "probe_disallows_media_asr_cloud_or_compute"
        elif cap is None or platform not in cap.get("sources", []):
            reason = "probe_unknown_route"
        elif "browser" in gates and not options.enable_browser_backends:
            reason = "browser_gate_closed"
        elif "network" in gates and not options.enable_network_backends:
            reason = "network_gate_closed"
        if reason:
            excluded.append({"branch": backend, "reason": reason})
        else:
            safe.append(backend)
    return tuple(safe), excluded


def probe_platform(platform: str, sample: ProbeSample, options: BackendRuntimeOptions) -> ProbeResult:
    """One bounded sample; exceptions are isolated by the outer report runner."""
    row = ProbeResult(platform=platform, configured=True, mode=sample.mode)
    if detect_platform(sample.url) != platform:
        row.reason = row.error_code = "sample_platform_mismatch"
        return row
    # URL-based sample resolution and discovery require an explicit gate. In
    # particular, do not let locally registered metadata providers fake health.
    if not (options.enable_network_backends or options.enable_browser_backends):
        row.reason = row.error_code = "probe_network_gates_closed"
        return row
    if sample.mode == "manifest":
        runtime = BackendRuntimeOptions(
            requested_discovery_provider=sample.discovery_backend,
            enable_network_backends=options.enable_network_backends,
            enable_browser_backends=options.enable_browser_backends,
        )
        providers = create_runtime_discovery_provider_registry(platform, runtime)
        providers.allow_skeleton_fallback = False
        request = DiscoveryRequest(input_url=sample.url, platform=platform, limit=1, allow_all=False)
        provider = providers.provider_for(request, requested_provider=sample.discovery_backend)
        provider_name = getattr(provider, "name", "not_selected")
        gate = _BOUNDED_DISCOVERY.get(platform, {}).get(provider_name)
        if gate is None or not getattr(options, f"enable_{gate}_backends", False):
            row.branch = provider_name
            row.reason = row.error_code = "probe_discovery_not_bounded_or_gate_closed"
            return row
        result = DiscoveryExecutor(provider_registry=providers, requested_provider=sample.discovery_backend).discover(
            request
        )
        row.branch = result.provider
        row.item_count = len(result.items)
        row.error_code = _safe_reason(result.error_code, sample.url)
        row.reason = _safe_reason(result.error_summary, sample.url)
        row.status = "ok" if result.ok and result.items and result.provider not in {"mock.discovery", "skeleton.discovery"} else "broken"
        invalid_sample = result.error_code in {
            "douyin_single_video_not_supported_for_discovery", "douyin_short_link_resolution_required", "douyin_user_sec_uid_not_found",
        }
        row.live_route_attempted = not invalid_sample
        if invalid_sample:
            row.status = "degraded"
            row.reason = "missing_valid_probe_sample; configured selector is unsupported for bounded discovery; live route not covered"
        if row.status == "broken" and not row.reason:
            row.reason = row.error_code = "probe_manifest_empty_or_non_live"
        row.attempts.append({"branch": row.branch, "status": row.status, "error_code": row.error_code, "reason": row.reason})
        return row

    if platform == "douyin":
        # Health probing reads one work's detail only. It never downloads media
        # or invokes ASR, so detail availability cannot certify transcription.
        from openmd.backends.douyin_detail import DouyinDetailError, DouyinDetailProvider

        row.branch = "opencli.douyin.detail"
        row.transcription = "not_probed_media_asr_disallowed"
        if not options.enable_browser_backends:
            row.reason = row.error_code = "browser_gate_closed"
            return row
        adapter = create_default_registry().get_adapter(platform)
        items = adapter.resolve_items(sample.url, limit=None, all=False)
        if len(items) != 1 or items[0].input_type != "single":
            row.reason = row.error_code = "probe_requires_single_item_or_manifest_mode"
            return row
        row.live_route_attempted = True
        try:
            DouyinDetailProvider().resolve(items[0])
            row.detail_readable = True
            row.item_count = 1
            row.status = "degraded"
            row.reason = "detail_readable; transcription_not_probed_media_asr_disallowed"
        except DouyinDetailError as exc:
            row.detail_readable = False
            row.error_code = exc.code
            row.reason = str(exc)
        row.attempts.append({"branch": row.branch, "status": row.status, "error_code": row.error_code, "reason": row.reason})
        return row

    adapters = create_default_registry()
    chain, row.excluded_branches = _safe_chain(platform, adapters.backend_chain(platform), options)
    if not chain:
        row.reason = row.error_code = "probe_no_safe_enabled_route"
        return row
    providers = create_runtime_backend_provider_registry(platform, options)
    providers.allow_skeleton_fallback = False
    items = adapters.get_adapter(platform).resolve_items(sample.url, limit=None, all=False)
    if len(items) != 1 or items[0].input_type == "collection":
        row.reason = row.error_code = "probe_requires_single_item_or_manifest_mode"
        return row
    executor = BackendExecutor(provider_registry=providers)
    for backend in chain:
        row.live_route_attempted = row.live_route_attempted or (
            backend not in {"douyin.metadata", "manual.markdown-export"} and providers.provider_for(backend) is not None
        )
        result = executor.extract(items[0], (backend,), allow_fallback=False)
        row.branch = result.backend
        row.extraction_status = result.status
        row.source_level = result.source_level
        row.route_version = result.metadata.get("route_version")
        row.tool_version = result.metadata.get("tool_run", {}).get("tool_version")
        row.error_code = _safe_reason(result.error_code, sample.url)
        row.reason = _safe_reason(result.error_summary, sample.url)
        row.text_chars = len(result.text)
        row.attempts.append({"branch": backend, "actual_branch": result.backend, "status": result.status, "error_code": row.error_code, "reason": row.reason})
        if result.has_usable_text:
            row.item_count = 1
            if result.metadata.get("metadata_only") or backend == "douyin.metadata":
                row.status = "degraded"
                row.error_code = "missing_valid_probe_sample"
                row.reason = "missing_valid_probe_sample; metadata_only_not_live_extraction; live route not covered"
            elif result.status == "partial" or result.has_terminal_gaps or len(row.attempts) > 1:
                row.status = "degraded"
                row.reason = row.reason or "partial_content_or_fallback_recovered"
            else:
                row.status = "ok"
            return row
    row.reason = row.reason or "probe_no_usable_content"
    return row


def _write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _probe_isolated(platform: str, sample: ProbeSample, options: BackendRuntimeOptions, timeout_seconds: int) -> ProbeResult:
    """Own the full provider subtree so one slow platform cannot stall all probes."""
    payload = {"platform": platform, "sample": asdict(sample), "network": options.enable_network_backends, "browser": options.enable_browser_backends}
    try:
        process = run_interruptibly(
            worker_command(Path(sys.executable), module="openmd.probe"),
            input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(platform=platform, configured=True, mode=sample.mode, error_code="probe_timeout", reason=f"platform exceeded {timeout_seconds}s; owned subprocess tree reclaimed")
    if process.returncode:
        return ProbeResult(platform=platform, configured=True, mode=sample.mode, error_code="probe_worker_failed", reason=f"probe worker exit {process.returncode}")
    try:
        return ProbeResult(**json.loads(process.stdout))
    except (ValueError, TypeError):
        return ProbeResult(platform=platform, configured=True, mode=sample.mode, error_code="probe_worker_invalid_result", reason="probe worker returned an invalid report")


def run_probe(
    samples: dict[str, ProbeSample], *, out: Path, options: BackendRuntimeOptions,
    platforms: tuple[str, ...] = PLATFORM_IDS, interval_seconds: float = 1.0, timeout_seconds: int = 90,
) -> tuple[dict[str, Any], Path]:
    now = datetime.now(timezone.utc)
    directory = out / now.astimezone().date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"probe-{now.strftime('%H%M%S-%f')}.json"
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "started_at": now.isoformat(), "completed": False,
        "route_version": load_catalog().version,
        "gates": {"network": options.enable_network_backends, "browser": options.enable_browser_backends, "media_download": False, "asr": False, "cloud": False},
        "sample_policy": "one configured sample per platform; no URL or source content persisted",
        "rate_limit_seconds": interval_seconds, "timeout_seconds": timeout_seconds, "results": [],
    }
    _write_report(path, report)
    attempted = 0
    # Rebuild options instead of trusting a caller to leave the heavy gates shut.
    bounded_options = BackendRuntimeOptions(enable_network_backends=options.enable_network_backends, enable_browser_backends=options.enable_browser_backends)
    for platform in platforms:
        sample = samples.get(platform)
        if sample is None:
            row = ProbeResult(platform=platform, configured=False, status="degraded", reason="sample_not_configured")
        else:
            if attempted:
                time.sleep(max(0.0, interval_seconds))
            attempted += 1
            started = time.monotonic()
            report["current_platform"] = platform
            _write_report(path, report)
            try:
                row = _probe_isolated(platform, sample, bounded_options, timeout_seconds)
            except Exception as exc:
                row = ProbeResult(platform=platform, configured=True, mode=sample.mode, reason=_safe_reason(exc, sample.url), error_code="probe_platform_error")
            row.elapsed_seconds = round(time.monotonic() - started, 3)
        report["results"].append(asdict(row))
        _write_report(path, report)  # crash/cancel preserves completed platforms
    report["summary"] = {status: sum(r["status"] == status for r in report["results"]) for status in ("ok", "degraded", "broken")}
    report["configured_count"] = attempted
    report["ok"] = attempted > 0 and all(row["status"] == "ok" for row in report["results"] if row["configured"])
    report["completed"] = True
    report.pop("current_platform", None)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_report(path, report)
    return report, path


def cmd_probe(args: argparse.Namespace) -> int:
    config = Path(args.config).expanduser() if args.config else default_config_path()
    samples = load_probe_samples(config, required=bool(args.config or os.environ.get("OPENMD_PROBE_CONFIG")))
    platforms = (args.platform,) if args.platform else PLATFORM_IDS
    report, path = run_probe(
        samples, out=Path(args.out), platforms=platforms,
        options=BackendRuntimeOptions(enable_network_backends=args.enable_network_backends, enable_browser_backends=args.enable_browser_backends),
        timeout_seconds=args.timeout,
    )
    if args.json:
        print(json.dumps({**report, "report_path": str(path)}, ensure_ascii=False))
    else:
        for row in report["results"]:
            print(f"{row['platform']}: {row['status']} | {row['branch'] or '-'} | {row['reason'] or '-'}")
        print(f"报告：{path}")
    return 0 if report["ok"] else 1


def _worker_main() -> None:
    payload = json.loads(sys.stdin.read())
    sample = ProbeSample(**payload["sample"])
    platform = payload["platform"]
    try:
        # Provider diagnostics never share stdout with the sanitized wire result.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            row = probe_platform(platform, sample, BackendRuntimeOptions(enable_network_backends=payload["network"], enable_browser_backends=payload["browser"]))
    except Exception as exc:
        row = ProbeResult(platform=platform, configured=True, mode=sample.mode, error_code="probe_platform_error", reason=_safe_reason(exc, sample.url))
    sys.stdout.buffer.write(json.dumps(asdict(row), ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    _worker_main()
