"""Argparse-based Openmd CLI skeleton."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.douyin import DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND
from openmd.backends.douyin_providers import DOUYIN_TRANSCRIPT_BACKEND
from openmd.backends.xiaohongshu import OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND
from openmd.backends.executor import (
    BackendExecutor,
    DiscoveryExecutor,
)
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
    normalize_discovery_backend,
)
from openmd.backends.youtube_discovery import (
    YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    is_youtube_collection_candidate,
    opencli_youtube_playlist_target,
    youtube_data_api_playlist_id,
)
from openmd.core.detector import PLATFORM_IDS, detect_platform
from openmd.core.coverage import CollectionCoverage, collection_content_type_counts
from openmd.core.capabilities import filter_capabilities
from openmd.core.doctor import DOCTOR_PROFILES, build_doctor_report
from openmd.core.manifest import write_discovery_manifest
from openmd.core.models import ContentItem
from openmd.core.output import write_item_result
from openmd.core.package_contract import (
    apply_platform_extraction_contract,
    extraction_contract_version_for,
    required_component_ids_for_item,
    package_metadata_satisfies_extraction_contract,
    package_source_level,
)
from openmd.core.package_writer import find_corrupt_package_for_item, find_existing_package_for_item, write_item_package
from openmd.core.package_validator import PackageValidationError, validate_package_or_raise
from openmd.core.planner import build_plan
from openmd.core.operation_contract import collect_all_allowed
from openmd.core.receipt import CompactReceiptRun
from openmd.core.execution_policy import CompactOutputLock, execution_fingerprint, require_matching_fingerprint
from openmd.core.secrets import sanitize_url
from openmd.core.runlog import (
    append_runlog_entry,
    build_item_runlog_entry,
    build_runlog_entry,
    default_runlog_path,
)
from openmd.core.secrets import sanitize_error_summary

DEFAULT_GUARDRAIL_ITEM_THRESHOLD = 100
DEFAULT_GUARDRAIL_FAILURE_STREAK = 10


@dataclass
class CliResult:
    ok: bool
    command: str
    item_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    outputs: list[str] = field(default_factory=list)
    guardrail_signals: list[str] = field(default_factory=list)
    source_level_counts: dict[str, int] = field(default_factory=dict)
    failure_code_counts: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, object] = field(default_factory=dict)
    message: str = ""
    paused_count: int = 0
    error_code: str | None = None
    login: dict[str, str] | None = None
    next_action: dict[str, str] | str | None = None
    status: str = "failed"
    source_level: str | None = None
    completeness: dict[str, object] = field(default_factory=dict)
    sync: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputWriteResult:
    path: Path
    package_id: str | None = None
    package_operation: str | None = None


ADVANCED_COMMANDS = ("ingest", "collect", "doctor", "probe", "routes", "report", "capabilities", "plan", "browser-setup", "asr")
GO_DAILY_OPTIONS = {"help", "url", "save_to", "video", "raw", "json", "verbose"}


def _runlog_path(args: argparse.Namespace) -> Path:
    return Path(args.runlog) if args.runlog else default_runlog_path(args.out)


def _print_result(result: CliResult) -> None:
    # Keep CLI status JSON ASCII-safe for Windows consoles with narrow encodings.
    print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))


def _read_urls_file(path: str) -> list[str]:
    lines = sys.stdin.read().splitlines() if path == "-" else Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _read_single_input(value: str) -> str:
    if value != "-":
        return value
    inputs = [line.strip() for line in sys.stdin.read().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(inputs) != 1:
        raise ValueError("stdin must contain exactly one non-comment input")
    return inputs[0]


def _log_all_guard(input_url: str, args: argparse.Namespace, *, command: str) -> CliResult:
    platform = detect_platform(input_url)
    entry = build_runlog_entry(
        input_url=input_url,
        platform=platform,
        backend=args.backend or "not_selected",
        status="failed",
        source_type="mixed",
        source_level="partial_extraction",
        item_count=0,
        success_count=0,
        failed_count=1,
        event_type="guardrail",
        failure_class="user_resolvable",
        guardrail_signals=["all_requires_explicit_gate"],
        resume_pointer="rerun with an explicitly supported provider gate",
        error_code="all_requires_confirmation",
        error_summary="--all requires an explicitly supported provider gate before collection work starts.",
    )
    _append_execution_entry(args, entry)
    return CliResult(
        ok=False,
        command=command,
        failed_count=1,
        guardrail_signals=["all_requires_explicit_gate"],
        message="--all requires explicit confirmation before any collection work.",
    )


def _allow_scoped_all_collect(input_url: str, args: argparse.Namespace) -> bool:
    """Allow full collection only for explicitly gated lanes that support it."""
    if not getattr(args, "all", False):
        return True
    return collect_all_allowed(
        _detect_collect_platform(input_url),
        browser_enabled=bool(getattr(args, "enable_browser_backends", False)),
        network_enabled=bool(getattr(args, "enable_network_backends", False)),
        discovery_provider=normalize_discovery_backend(getattr(args, "discovery_backend", None)),
    )


def _build_backend_executor(args: argparse.Namespace, *, platform: str) -> BackendExecutor:
    from openmd.backends.video_metadata import enrich_video_result

    provider_registry = create_runtime_backend_provider_registry(
        platform,
        _backend_runtime_options(args),
    )
    return BackendExecutor(
        provider_registry=provider_registry,
        result_enricher=lambda item, result: enrich_video_result(
            item, result, network_enabled=bool(getattr(args, "enable_network_backends", False))
        ),
    )


def _build_discovery_executor(args: argparse.Namespace, *, platform: str) -> DiscoveryExecutor:
    options = _backend_runtime_options(args)
    provider_registry = create_runtime_discovery_provider_registry(platform, options)
    return DiscoveryExecutor(
        provider_registry=provider_registry,
        requested_provider=normalize_discovery_backend(options.requested_discovery_provider),
    )


def _backend_runtime_options(args: argparse.Namespace) -> BackendRuntimeOptions:
    keep_media_artifacts = getattr(args, "keep_media", False)
    return BackendRuntimeOptions(
        backend_override=getattr(args, "backend", None),
        requested_discovery_provider=getattr(args, "discovery_backend", None),
        enable_network_backends=getattr(args, "enable_network_backends", False),
        enable_browser_backends=getattr(args, "enable_browser_backends", False),
        enable_media_download_backends=getattr(args, "enable_media_download", False),
        enable_asr_backends=getattr(args, "enable_asr", False),
        enable_cloud_backends=getattr(args, "enable_cloud_backends", False),
        enable_local_export_backends=getattr(args, "enable_local_export", False),
        keep_media_artifacts=keep_media_artifacts,
        media_artifact_dir=str(Path(args.out) / "raw-media") if keep_media_artifacts else None,
    )


def _select_collect_discovery_backend(input_url: str, args: argparse.Namespace) -> None:
    if getattr(args, "discovery_backend", None):
        return
    if getattr(args, "enable_network_backends", False) and youtube_data_api_playlist_id(input_url) is not None:
        args.discovery_backend = YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND
        return
    if not getattr(args, "enable_browser_backends", False):
        return
    if _detect_collect_platform(input_url) == "douyin" and getattr(args, "all", False):
        args.discovery_backend = DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND
        if not getattr(args, "backend", None):
            args.backend = DOUYIN_TRANSCRIPT_BACKEND
        return
    if _detect_collect_platform(input_url) == "xiaohongshu" and getattr(args, "all", False):
        args.discovery_backend = OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND
        return
    if opencli_youtube_playlist_target(input_url) is not None:
        args.discovery_backend = "youtube-playlist"


def _requested_backend(platform: str, args: argparse.Namespace) -> str | None:
    """Explicit --backend wins; local media under --enable-asr picks its model by language."""
    if getattr(args, "backend", None):
        return args.backend
    if platform == "generic_media" and getattr(args, "enable_asr", False):
        return "asr.auto"
    return None


def _detect_collect_platform(input_url: str) -> str:
    platform = detect_platform(input_url)
    if platform == "web_article" and is_youtube_collection_candidate(input_url):
        return "youtube"
    return platform


def _pace_douyin_extraction(item: ContentItem, args: argparse.Namespace) -> None:
    """Share one bounded cadence across collection and multi-URL batch items."""
    if item.platform != "douyin" or getattr(args, "backend", None) in {"mock", "douyin.metadata"}:
        return
    previous = getattr(args, "_douyin_last_extraction_at", None)
    if previous is not None:
        time.sleep(max(0.0, 1.0 - (time.monotonic() - previous)))
    args._douyin_last_extraction_at = time.monotonic()


def main(argv: list[str] | None = None) -> int:
    from openmd.console import configure_console

    configure_console()
    actual = list(sys.argv[1:] if argv is None else argv)
    if not actual:
        from openmd.interactive import is_interactive

        if is_interactive():
            actual = ["go"]
    elif actual[0] not in {"ingest", "go", "batch", "collect", "capabilities", "plan", "doctor", "probe", "routes", "report", "browser-setup", "asr", "opencli", "models"} and not actual[0].startswith("-"):
        actual.insert(0, "go")
    parser = build_parser()
    try:
        args = parser.parse_args(actual)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    from openmd.runtime.managed_tools import activate as activate_managed_tools

    if getattr(args, "command", None) != "doctor":
        from openmd.runtime.tempdirs import sweep_at_startup

        sweep_at_startup()
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    if getattr(args, "package", False):
        if getattr(args, "receipt", None) == "compact" and any(
            (
                getattr(args, "update", False),
                getattr(args, "overwrite", False),
                getattr(args, "new_version", False),
            )
        ):
            print(
                "openmd: --receipt compact cannot be combined with --update, --overwrite, or --new-version",
                file=sys.stderr,
            )
            return 1
        if getattr(args, "new_version", False) and (
            getattr(args, "update", False) or getattr(args, "overwrite", False)
        ):
            print(
                "openmd: --new-version cannot be combined with --update or --overwrite",
                file=sys.stderr,
            )
            return 1
    try:
        if getattr(args, "command", None) != "doctor":
            activate_managed_tools()  # activate within the normal CLI error boundary
        return args.func(args)
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("已取消。", file=sys.stderr)
            if getattr(args, "receipt", None) != "compact":
                return 130
        if getattr(args, "receipt", None) == "compact":
            args._compact_execution_failed = not isinstance(exc, KeyboardInterrupt)
            if getattr(args, "_compact_receipt_run", None) is not None:
                return _emit_result(args, CliResult(ok=False, command=args.command, message="execution_interrupted"), discovery_complete=False)
            return _print_compact_failure()
        print(f"openmd: {sanitize_error_summary(exc)}", file=sys.stderr)
        return 1
    finally:
        from openmd.runtime.services import finish_openmd_bridge
        finish_openmd_bridge()
        lock = getattr(args, "_compact_output_lock", None)
        if lock is not None:
            try:
                lock.release()
            except OSError:
                pass  # The bounded failure path already reports failure; leave residual lock for inspection.


from .cli_parser import build_parser, _add_shared_options
from .cli_receipts import _start_compact_receipt, _compact_failure_payload, _print_compact_failure, _register_compact_inputs, _sync_compact_rows, _append_execution_entry, _prepare_compact_collection, _reconcile_compact_identity, _register_resolved_batch_items, _emit_result, _read_new_runlog_rows, _compact_item_identity, _compact_url_identity, _prepare_resolved_item, _record_compact_rows, _compact_terminal_status, _compact_execution_fingerprint
from .cli_items import _process_items, _process_input, _maybe_skip_existing_package, _write_extraction_output, _build_package_validation_failure_entry, _build_identity_resolution_failure_entry, _build_package_validation_failure_entry_for_item, _build_skipped_package_runlog_entry, _read_package_metadata
from .cli_commands import cmd_doctor, cmd_routes, cmd_capabilities, cmd_plan, cmd_report_weixin_author_batch, cmd_ingest, cmd_batch
from .cli_collections import _write_youtube_discovery_summary, cmd_collect, _write_douyin_coverage_summary, _package_coverage_source_level_counts, _write_xiaohongshu_coverage_summary, _xiaohongshu_component_coverage, _safe_count_mapping, _optional_non_negative_int, _non_negative_int, _guardrail_item_threshold, _guardrail_failure_streak, _guardrail_failure_streak_reached, _append_large_batch_guardrail_if_needed, _append_guardrail_stop_entry

if __name__ == "__main__":
    raise SystemExit(main())
