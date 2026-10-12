"""Argparse-based Openmd CLI skeleton."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
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
from openmd.core.detector import detect_platform
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


@dataclass(frozen=True)
class OutputWriteResult:
    path: Path
    package_id: str | None = None
    package_operation: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmd",
        description="Markdown-first content ingestion CLI. Official command entry: openmd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Common examples:\n"
            "  openmd ingest <url> --out out\n"
            "  Get-Clipboard | openmd ingest - --out out\n"
            "  openmd collect <target> --manifest-only --out out\n"
            "  openmd batch urls.txt --package --out out"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    ingest = subparsers.add_parser("ingest", help="ingest one input")
    ingest.add_argument("url")
    _add_shared_options(ingest)
    ingest.set_defaults(func=cmd_ingest)

    batch = subparsers.add_parser("batch", help="ingest inputs from a text file")
    batch.add_argument("urls_file")
    _add_shared_options(batch)
    batch.set_defaults(func=cmd_batch)

    collect = subparsers.add_parser("collect", help="discover a target, then optionally ingest its items")
    collect.add_argument("url")
    _add_shared_options(
        collect,
        default_limit=20,
        include_manifest_only=True,
        include_discovery_backend=True,
    )
    collect.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="controlled extraction slice size after full discovery; 0 processes the remaining collection",
    )
    collect.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="zero-based extraction slice start after full discovery; default: 0",
    )
    collect.add_argument(
        "--progress",
        action="store_true",
        help="write sanitized per-item progress heartbeats to stderr",
    )
    collect.set_defaults(func=cmd_collect)

    capabilities = subparsers.add_parser("capabilities", help="show read-only capability bindings")
    capabilities.add_argument("--source", help="filter by normalized source id")
    capabilities.add_argument("--format", choices=("json", "table"), default="table")
    capabilities.set_defaults(func=cmd_capabilities)

    plan = subparsers.add_parser("plan", help="build a read-only capability plan")
    plan.add_argument("input")
    plan.add_argument("--input-kind", choices=("auto", "single", "list", "collection", "file", "folder"), default="auto")
    plan.add_argument("--all", action="store_true", help="request source-exhausted planning only")
    plan.add_argument("--gate", action="append", default=[], help="declare a planning gate without executing a provider")
    plan.add_argument("--format", choices=("json", "table"), default="table")
    plan.set_defaults(func=cmd_plan)

    doctor = subparsers.add_parser(
        "doctor",
        help="print local entry and safe session-boundary diagnostics",
    )
    doctor.add_argument("--profile", choices=DOCTOR_PROFILES, default="core")
    doctor.set_defaults(func=cmd_doctor)

    report = subparsers.add_parser("report", help="build reports from sanitized runlogs")
    report_subparsers = report.add_subparsers(dest="report_command")
    weixin_author_batch = report_subparsers.add_parser(
        "weixin-author-batch",
        help="summarize Weixin author-batch coverage, recovered fallback, and missing items",
    )
    weixin_author_batch.add_argument(
        "--runlog",
        action="append",
        required=True,
        help="sanitized runlog JSONL path; repeat to merge multiple runs",
    )
    weixin_author_batch.add_argument("--out", required=True, help="report output directory")
    weixin_author_batch.add_argument(
        "--format",
        choices=("both", "json", "markdown"),
        default="both",
        help="report artifact format",
    )
    weixin_author_batch.set_defaults(func=cmd_report_weixin_author_batch)
    return parser


def _add_shared_options(
    parser: argparse.ArgumentParser,
    *,
    default_limit: int | None = None,
    include_manifest_only: bool = False,
    include_discovery_backend: bool = False,
) -> None:
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--backend", help="backend name override")
    parser.add_argument("--fallback", dest="fallback", action="store_true", default=True)
    parser.add_argument("--no-fallback", dest="fallback", action="store_false")
    parser.add_argument("--limit", type=int, default=default_limit)
    parser.add_argument(
        "--all",
        action="store_true",
        help="full collection; only runs for explicitly gated providers",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--package",
        action="store_true",
        help="write V1 item.openmd/ package artifacts instead of V0 single Markdown",
    )
    parser.add_argument(
        "--package-root",
        help="package library root; defaults to <out>/packages when --package is used",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip existing package_id matches; package mode uses this behavior by default",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="update an existing package in place when package_id already exists",
    )
    parser.add_argument(
        "--new-version",
        action="store_true",
        help="create a versioned package snapshot instead of updating/skipping",
    )
    parser.add_argument("--download-assets", action="store_true")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--keep-media", action="store_true")
    parser.add_argument(
        "--enable-media-download",
        action="store_true",
        help="opt in to media download fallback backends; raw media is still deleted unless --keep-media is set",
    )
    parser.add_argument(
        "--enable-asr",
        action="store_true",
        help="opt in to ASR fallback backends after native text/subtitle paths fail",
    )
    parser.add_argument(
        "--enable-cloud-backends",
        action="store_true",
        help="explicitly allow cloud ASR/OCR providers; --enable-asr alone does not allow upload",
    )
    parser.add_argument(
        "--guardrail-item-threshold",
        type=int,
        default=DEFAULT_GUARDRAIL_ITEM_THRESHOLD,
        help="emit a runlog guardrail signal when a batch/collection reaches this item count; 0 disables",
    )
    parser.add_argument(
        "--guardrail-failure-streak",
        type=int,
        default=DEFAULT_GUARDRAIL_FAILURE_STREAK,
        help="pause batch/collection after this many consecutive failed items; 0 disables",
    )
    if include_manifest_only:
        parser.add_argument(
            "--manifest-only",
            action="store_true",
            help="write discovery manifest and skip item extraction",
        )
    if include_discovery_backend:
        parser.add_argument(
            "--discovery-backend",
            help="discovery provider override for collect, independent from --backend",
        )
    parser.add_argument("--runlog", help="runlog JSONL path; defaults to <out>/runlog.jsonl")
    parser.add_argument(
        "--receipt",
        choices=("compact",),
        help="write a bounded per-run receipt and print one compact JSON line",
    )
    parser.add_argument(
        "--enable-network-backends",
        action="store_true",
        help="opt in to real network backends such as youtube-transcript-api",
    )
    parser.add_argument(
        "--enable-browser-backends",
        action="store_true",
        help="opt in to browser-context backends such as OpenCLI for matching collection inputs",
    )


def _runlog_path(args: argparse.Namespace) -> Path:
    return Path(args.runlog) if args.runlog else default_runlog_path(args.out)


def _print_result(result: CliResult) -> None:
    # Keep CLI status JSON ASCII-safe for Windows consoles with narrow encodings.
    print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))


def _start_compact_receipt(args: argparse.Namespace, command: str) -> str | None:
    if getattr(args, "receipt", None) != "compact":
        return None
    try:
        run = CompactReceiptRun.create(args.out, command)
    except OSError:
        args._compact_receipt_storage_unavailable = True
        return "compact receipt storage is unavailable"
    runlog_path = _runlog_path(args)
    args._compact_receipt_run = run
    args._compact_runlog_path = runlog_path
    args._compact_runlog_offset = runlog_path.stat().st_size if runlog_path.exists() else 0
    args._compact_requires_fresh_stdin = (
        getattr(args, "url", None) == "-" or getattr(args, "urls_file", None) == "-"
    )
    run.requires_fresh_stdin = args._compact_requires_fresh_stdin
    run.write_checkpoint()
    if getattr(args, "package", False):
        try:
            args._compact_output_lock = CompactOutputLock.acquire(args.out, run.run_id)
        except FileExistsError:
            return "compact package output is locked; inspect .openmd-compact.lock before retrying"
    return None


def _compact_failure_payload() -> dict[str, object]:
    """Return the bounded receipt used when the compact run cannot be persisted."""
    counts = {status: 0 for status in CompactReceiptRun.TERMINAL_STATUSES}
    counts["failed"] = 1
    counts["discovered"] = 1
    counts["skipped"] = 0
    counts["input_occurrences"] = 1
    counts["duplicate_inputs"] = 0
    return {
        "schema_version": "openmd.receipt.v1",
        "status": "failed",
        "counts": counts,
        "summary_ref": None,
        "error_counts": {"unknown": 1},
        "other_error_count": 0,
    }


def _print_compact_failure() -> int:
    print(json.dumps(_compact_failure_payload(), ensure_ascii=False, separators=(",", ":")))
    return 1


def _register_compact_inputs(args: argparse.Namespace, identities: dict[str, int]) -> None:
    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        return
    args._compact_inputs_registered = True
    args._compact_pending_identities = identities
    run.replace_expected(identities)


def _sync_compact_rows(args: argparse.Namespace) -> None:
    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        return
    path = args._compact_runlog_path
    rows = _read_new_runlog_rows(path, args._compact_runlog_offset)
    _record_compact_rows(run, rows, args.out, args=args)
    args._compact_runlog_offset = path.stat().st_size if path.exists() else 0


def _append_execution_entry(args: argparse.Namespace, entry: dict[str, object]) -> None:
    append_runlog_entry(_runlog_path(args), entry)
    # Receipt/checkpoint must be durable before the next provider call.
    _sync_compact_rows(args)


def _prepare_compact_collection(args: argparse.Namespace, items: list[ContentItem]) -> list[ContentItem]:
    if getattr(args, "receipt", None) != "compact":
        return items
    unique: dict[str, ContentItem] = {}
    occurrences: dict[str, int] = {}
    by_url: dict[str, int] = {}
    for item in items:
        identity = _compact_item_identity(item)
        unique.setdefault(identity, item)
        occurrences[identity] = occurrences.get(identity, 0) + 1
        by_url[sanitize_url(item.url)] = occurrences[identity]
    args._compact_input_occurrences_by_url = by_url
    _register_compact_inputs(args, occurrences)
    return list(unique.values())


def _reconcile_compact_identity(args: argparse.Namespace, original: ContentItem, resolved: ContentItem) -> None:
    """A resolver may replace a short locator with the canonical resource URL."""
    if getattr(args, "_compact_receipt_run", None) is None:
        return
    previous, current = _compact_item_identity(original), _compact_item_identity(resolved)
    if previous == current:
        return
    pending = getattr(args, "_compact_pending_identities", {})
    occurrence = pending.pop(previous, 1)
    pending[current] = pending.get(current, 0) + occurrence
    run = args._compact_receipt_run
    expected = dict(run.expected)
    old_occurrence = expected.pop(previous, occurrence)
    expected[current] = expected.get(current, 0) + old_occurrence
    run.add_occurrences(current, old_occurrence)
    run.replace_expected(expected)
    by_url = getattr(args, "_compact_input_occurrences_by_url", {})
    by_url[sanitize_url(resolved.url)] = expected[current]
    args._compact_input_occurrences_by_url = by_url


def _register_resolved_batch_items(args: argparse.Namespace, input_url: str, items: list[ContentItem]) -> None:
    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        return
    previous = _compact_url_identity(input_url)
    pending = args._compact_pending_identities
    occurrence = pending.get(previous, 1)
    if not items:
        if not run.has_item(previous):
            run.record_item(stable_identity=previous, input_occurrence=occurrence,
                            status="blocked", error_code="blocked", recovery_type="empty_scope")
        return
    pending.pop(previous, None)
    expected = dict(run.expected)
    expected.pop(previous, None)
    by_url = args._compact_input_occurrences_by_url
    for item in items:
        identity = _compact_item_identity(item)
        pending[identity] = pending.get(identity, 0) + occurrence
        expected[identity] = expected.get(identity, 0) + occurrence
        run.add_occurrences(identity, occurrence)
        by_url[sanitize_url(item.url)] = expected[identity]
    run.replace_expected(expected)


def _emit_result(
    args: argparse.Namespace,
    result: CliResult,
    *,
    discovery_complete: bool = True,
    manifest_only: bool = False,
) -> int:
    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        if getattr(args, "_compact_receipt_storage_unavailable", False):
            return _print_compact_failure()
        _print_result(result)
        return 0 if result.ok else 1
    try:
        if not manifest_only:
            _sync_compact_rows(args)
        for identity, occurrence in getattr(args, "_compact_pending_identities", {}).items():
            if not run.has_item(identity):
                run.record_item(
                    stable_identity=identity, input_occurrence=occurrence,
                    status="unattempted", quality_status="not_evaluated",
                    recovery_type="manifest_only" if manifest_only else "rerun_input",
                )
        if not run.has_items and not result.ok and not getattr(args, "_compact_inputs_registered", False):
            run.record_item(
                stable_identity=CompactReceiptRun.stable_identity(result.command, "preflight"),
                input_occurrence=1, status="blocked", error_code="blocked",
                recovery_type="inspect_runlog",
            )
        payload, exit_code = run.finalize(
            discovery_complete=discovery_complete,
            manifest_only=manifest_only,
            resume_supported=bool(getattr(args, "package", False)),
            requires_fresh_stdin=bool(getattr(args, "_compact_requires_fresh_stdin", False)),
            failed=bool(getattr(args, "_compact_execution_failed", False)),
        )
        lock = getattr(args, "_compact_output_lock", None)
        if lock is not None:
            lock.release()
            args._compact_output_lock = None
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return exit_code
    except (OSError, ValueError):
        return _print_compact_failure()


def _read_new_runlog_rows(path: Path, offset: int) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        lines = handle.read().splitlines()
    rows: list[dict[str, object]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed_runlog_line") from exc
        if isinstance(value, dict) and value.get("event_type") == "item":
            rows.append(value)
    return rows


def _compact_item_identity(item: ContentItem) -> str:
    return CompactReceiptRun.stable_identity(item.platform, sanitize_url(item.url))


def _compact_url_identity(url: str) -> str:
    return CompactReceiptRun.stable_identity(detect_platform(url), sanitize_url(url))


def _prepare_resolved_item(
    args: argparse.Namespace,
    original: ContentItem,
    resolved: ContentItem,
    provider_chain: tuple[str, ...],
) -> tuple[ContentItem, str | None] | None:
    """Return the contract-applied item and its fingerprint, or None if the receipt already has it."""
    item = apply_platform_extraction_contract(resolved)
    _reconcile_compact_identity(args, original, item)
    run = getattr(args, "_compact_receipt_run", None)
    if run is not None and run.has_item(_compact_item_identity(item)):
        return None
    return item, _compact_execution_fingerprint(item, args, provider_chain)


def _record_compact_rows(
    run: CompactReceiptRun,
    rows: list[dict[str, object]],
    out_dir: str,
    *,
    args: argparse.Namespace,
) -> set[str]:
    out_root = Path(out_dir).resolve()
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        identity = CompactReceiptRun.stable_identity(
            str(row.get("platform") or ""),
            sanitize_url(str(row.get("input_url") or "")),
        )
        grouped.setdefault(identity, []).append(row)
    for identity, grouped_rows in grouped.items():
        if run.has_item(identity):
            continue
        row = grouped_rows[-1]
        status = _compact_terminal_status(row)
        output_path = str(row.get("output_path") or "")
        artifact_ref: str | None = None
        if output_path:
            try:
                artifact_ref = Path(output_path).resolve().relative_to(out_root).as_posix()
            except ValueError:
                artifact_ref = None
        run.record_item(
            stable_identity=identity,
            input_occurrence=max(
                len(grouped_rows),
                int(getattr(args, "_compact_input_occurrences_by_url", {}).get(str(row.get("input_url") or ""), 1)),
            ),
            status=status,
            skipped=str(row.get("package_operation") or "") == "skipped_existing",
            artifact_ref=artifact_ref,
            source_level=str(row.get("source_level") or "unknown"),
            quality_status="passed" if status == "verified" else "not_evaluated",
            error_code=str(row.get("error_code") or "") or None,
            failure_class=str(row.get("failure_class") or "") or None,
            recovery_type=str(row.get("resume_pointer") or "") or None,
        )
    return set(grouped)


def _compact_terminal_status(row: dict[str, object]) -> str:
    if int(row.get("terminal_gap_count") or 0) > 0:
        return "terminal_gap"
    status = str(row.get("status") or "failed")
    if status in {"verified", "partial"}:
        return status
    failure_class = str(row.get("failure_class") or "")
    if str(row.get("error_code") or "") in {
        "execution_fingerprint_missing",
        "execution_fingerprint_mismatch",
        "extraction_contract_mismatch",
        "package_validation_failed",
    }:
        return "blocked"
    if failure_class == "runtime_transient":
        return "retryable_failure"
    if failure_class == "source_unavailable":
        return "terminal_gap"
    if failure_class == "user_resolvable":
        return "blocked"
    return "failed"


def _compact_execution_fingerprint(
    item: ContentItem,
    args: argparse.Namespace,
    provider_chain: tuple[str, ...],
) -> str | None:
    if getattr(args, "receipt", None) != "compact" or not getattr(args, "package", False):
        return None
    raw_meta = item.raw_meta if isinstance(item.raw_meta, dict) else {}
    modalities_value = raw_meta.get("modalities")
    modalities = tuple(str(value) for value in modalities_value) if isinstance(modalities_value, list) else (item.source_type,)
    effective_config: dict[str, object] = {}
    if "asr.faster_whisper" in provider_chain:
        from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend

        effective_config["faster_whisper"] = FasterWhisperASRBackend().execution_config()
    if item.platform in {"douyin", "xiaohongshu"}:
        from openmd.backends.douyin_providers import SILICONFLOW_MODEL

        effective_config["cloud_asr_model"] = SILICONFLOW_MODEL
    return execution_fingerprint(
        requested_profile=str(raw_meta.get("profile") or "") or None,
        requested_modalities=modalities,
        provider_chain=provider_chain,
        backend_override=getattr(args, "backend", None),
        model=str(raw_meta.get("asr_model") or "") or None,
        language=str(raw_meta.get("language") or "") or None,
        extraction_contract={
            "version": extraction_contract_version_for(item),
            "required_components": list(required_component_ids_for_item(item)),
        },
        effective_config=effective_config,
    )


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
    provider_registry = create_runtime_backend_provider_registry(
        platform,
        _backend_runtime_options(args),
    )
    return BackendExecutor(provider_registry=provider_registry)


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


def _detect_collect_platform(input_url: str) -> str:
    platform = detect_platform(input_url)
    if platform == "web_article" and is_youtube_collection_candidate(input_url):
        return "youtube"
    return platform


def _process_items(
    items: list[ContentItem],
    args: argparse.Namespace,
    *,
    command: str,
) -> CliResult:
    registry = create_default_registry()
    outputs: list[str] = []
    success_count = 0
    failed_count = 0
    processed_count = 0
    consecutive_failures = 0
    executors: dict[str, BackendExecutor] = {}
    skipped_count = 0
    source_level_counts: dict[str, int] = {}
    failure_code_counts: dict[str, int] = {}
    coverage = CollectionCoverage()

    for index, item in enumerate(items, start=1):
        processed_count += 1
        selection = registry.select_backend(
            item.platform,
            args.backend,
            allow_fallback=args.fallback,
        )
        executor = executors.get(item.platform)
        if executor is None:
            executor = _build_backend_executor(args, platform=item.platform)
            executors[item.platform] = executor
        identity = executor.resolve_item_identity(item, selection.chain)
        if not identity.ok:
            failed_count += 1
            consecutive_failures += 1
            coverage.record_failure()
            code = identity.error_code or "identity_resolution_failed"
            failure_code_counts[code] = failure_code_counts.get(code, 0) + 1
            _append_execution_entry(
                args,
                _build_identity_resolution_failure_entry(item, identity),
            )
            if _guardrail_failure_streak_reached(args, consecutive_failures):
                _append_guardrail_stop_entry(args, command=command, index=index, total=len(items))
                break
            continue
        prepared = _prepare_resolved_item(args, item, identity.item, selection.chain)
        if prepared is None:
            continue
        item, fingerprint = prepared
        try:
            skipped_output = _maybe_skip_existing_package(item, args, execution_fingerprint=fingerprint)
        except PackageValidationError as exc:
            failed_count += 1
            consecutive_failures += 1
            coverage.record_failure()
            entry = _build_package_validation_failure_entry_for_item(item, exc)
            _append_execution_entry(args, entry)
            if _guardrail_failure_streak_reached(args, consecutive_failures):
                _append_guardrail_stop_entry(args, command=command, index=index, total=len(items))
                break
            continue
        if skipped_output is not None:
            outputs.append(str(skipped_output.path))
            success_count += 1
            skipped_count += 1
            skipped_metadata = _read_package_metadata(skipped_output.path)
            coverage.record_metadata(skipped_metadata, skipped_existing=True)
            skipped_source_level = str(skipped_metadata.get("source_level") or "unknown")
            source_level_counts[skipped_source_level] = source_level_counts.get(skipped_source_level, 0) + 1
            consecutive_failures = 0
            _append_execution_entry(
                args,
                _build_skipped_package_runlog_entry(item, skipped_output),
            )
            if getattr(args, "progress", False):
                print(
                    json.dumps(
                        {
                            "event": "item_progress",
                            "index": index,
                            "total": len(items),
                            "video_id": item.id,
                            "status": "verified",
                            "source_level": skipped_source_level,
                            "package_operation": "skipped_existing",
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            continue

        result = executor.extract(
            item,
            selection.chain,
            allow_fallback=args.fallback,
        )
        output: OutputWriteResult | None = None
        if result.has_usable_text:
            try:
                output = _write_extraction_output(item, result, args, execution_fingerprint=fingerprint)
            except PackageValidationError as exc:
                failed_count += 1
                consecutive_failures += 1
                coverage.record_result(result, force_failed=True)
                entry = _build_package_validation_failure_entry(item, result, exc)
                _append_execution_entry(args, entry)
                if _guardrail_failure_streak_reached(args, consecutive_failures):
                    _append_guardrail_stop_entry(args, command=command, index=index, total=len(items))
                    break
                continue
            else:
                outputs.append(str(output.path))
                success_count += 1
                source_level_counts[result.source_level] = source_level_counts.get(result.source_level, 0) + 1
                consecutive_failures = 0
        else:
            failed_count += 1
            consecutive_failures += 1
            code = result.error_code or "unknown_failure"
            failure_code_counts[code] = failure_code_counts.get(code, 0) + 1

        coverage.record_result(result)

        entry = build_item_runlog_entry(
            item,
            result,
            output_path=output.path if output else None,
            package_id=output.package_id if output else None,
            package_operation=output.package_operation if output else None,
        )
        _append_execution_entry(args, entry)
        if getattr(args, "progress", False):
            print(
                json.dumps(
                    {
                        "event": "item_progress",
                        "index": index,
                        "total": len(items),
                        "video_id": item.id,
                        "status": result.status,
                        "source_level": result.source_level,
                        "error_code": result.error_code,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
        if _guardrail_failure_streak_reached(args, consecutive_failures):
            _append_guardrail_stop_entry(args, command=command, index=index, total=len(items))
            break

    ok = failed_count == 0 and bool(items)
    return CliResult(
        ok=ok,
        command=command,
        item_count=processed_count,
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        outputs=outputs,
        guardrail_signals=["consecutive_failure_stop"] if processed_count < len(items) else [],
        source_level_counts=source_level_counts,
        failure_code_counts=failure_code_counts,
        coverage=coverage.as_dict(),
        message="completed" if ok else ("guardrail stopped" if processed_count < len(items) else "completed with failures"),
    )


def _process_input(input_url: str, args: argparse.Namespace, *, command: str) -> CliResult:
    if getattr(args, "all", False):
        return _log_all_guard(input_url, args, command=command)

    registry = create_default_registry()
    adapter = registry.adapter_for(input_url)
    selection = registry.select_backend(
        adapter.id,
        args.backend,
        allow_fallback=args.fallback,
    )
    items = [
        apply_platform_extraction_contract(item)
        for item in adapter.resolve_items(input_url, limit=args.limit, all=False)
    ]
    if command == "ingest":
        items = _prepare_compact_collection(args, items)
    elif command == "batch":
        _register_resolved_batch_items(args, input_url, items)
    executor = _build_backend_executor(args, platform=adapter.id)
    outputs: list[str] = []
    success_count = 0
    failed_count = 0
    skipped_count = 0

    for item in items:
        identity = executor.resolve_item_identity(item, selection.chain)
        if not identity.ok:
            failed_count += 1
            _append_execution_entry(
                args,
                _build_identity_resolution_failure_entry(item, identity),
            )
            continue
        prepared = _prepare_resolved_item(args, item, identity.item, selection.chain)
        if prepared is None:
            continue
        item, fingerprint = prepared
        try:
            skipped_output = _maybe_skip_existing_package(item, args, execution_fingerprint=fingerprint)
        except PackageValidationError as exc:
            failed_count += 1
            entry = _build_package_validation_failure_entry_for_item(item, exc)
            _append_execution_entry(args, entry)
            continue
        if skipped_output is not None:
            outputs.append(str(skipped_output.path))
            success_count += 1
            skipped_count += 1
            _append_execution_entry(
                args,
                _build_skipped_package_runlog_entry(item, skipped_output),
            )
            continue

        result = executor.extract(
            item,
            selection.chain,
            allow_fallback=args.fallback,
        )
        output: OutputWriteResult | None = None
        if result.has_usable_text:
            try:
                output = _write_extraction_output(item, result, args, execution_fingerprint=fingerprint)
            except PackageValidationError as exc:
                failed_count += 1
                entry = _build_package_validation_failure_entry(item, result, exc)
                _append_execution_entry(args, entry)
                continue
            else:
                outputs.append(str(output.path))
                success_count += 1
        else:
            failed_count += 1

        entry = build_item_runlog_entry(
            item,
            result,
            output_path=output.path if output else None,
            package_id=output.package_id if output else None,
            package_operation=output.package_operation if output else None,
        )
        _append_execution_entry(args, entry)

    ok = failed_count == 0 and bool(items)
    return CliResult(
        ok=ok,
        command=command,
        item_count=len(items),
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        outputs=outputs,
        message="completed" if ok else "completed with failures",
    )


def _maybe_skip_existing_package(
    item: ContentItem,
    args: argparse.Namespace,
    *,
    execution_fingerprint: str | None = None,
) -> OutputWriteResult | None:
    if not getattr(args, "package", False):
        return None
    if (
        getattr(args, "update", False)
        or getattr(args, "overwrite", False)
        or getattr(args, "new_version", False)
    ):
        return None
    if getattr(args, "receipt", None) == "compact":
        corrupt = find_corrupt_package_for_item(
            item,
            args.out,
            package_root=getattr(args, "package_root", None),
        )
        if corrupt is not None:
            raise PackageValidationError("package_validation_failed")
    existing = find_existing_package_for_item(
        item,
        args.out,
        package_root=getattr(args, "package_root", None),
    )
    if existing is None:
        return None
    validate_package_or_raise(existing.package_path)
    metadata = _read_package_metadata(existing.package_path)
    if execution_fingerprint:
        mismatch = require_matching_fingerprint(metadata, execution_fingerprint)
        if mismatch:
            raise PackageValidationError(mismatch)
    if not package_metadata_satisfies_extraction_contract(metadata, item):
        if getattr(args, "receipt", None) == "compact":
            raise PackageValidationError("extraction_contract_mismatch")
        return None
    return OutputWriteResult(
        path=existing.package_path,
        package_id=existing.package_id,
        package_operation=existing.operation,
    )


def _write_extraction_output(
    item: ContentItem,
    result,
    args: argparse.Namespace,
    *,
    execution_fingerprint: str | None = None,
) -> OutputWriteResult:
    if getattr(args, "package", False):
        package_result = write_item_package(
            item,
            result,
            args.out,
            package_root=getattr(args, "package_root", None),
            skip_existing=True,
            update=getattr(args, "update", False),
            overwrite=getattr(args, "overwrite", False),
            new_version=getattr(args, "new_version", False),
            execution_fingerprint=execution_fingerprint,
        )
        try:
            validate_package_or_raise(package_result.package_path)
        except PackageValidationError:
            if package_result.operation in {"created", "new_version"}:
                shutil.rmtree(package_result.package_path, ignore_errors=True)
            raise
        return OutputWriteResult(
            path=package_result.package_path,
            package_id=package_result.package_id,
            package_operation=package_result.operation,
        )

    return OutputWriteResult(
        path=write_item_result(
            item,
            result,
            args.out,
            overwrite=getattr(args, "overwrite", False),
            download_assets=getattr(args, "download_assets", False),
        )
    )


def _build_package_validation_failure_entry(
    item: ContentItem,
    result,
    exc: PackageValidationError,
) -> dict:
    return build_runlog_entry(
        input_url=item.url,
        platform=item.platform,
        backend=result.backend,
        status="failed",
        source_type=item.source_type,
        source_level=result.source_level,
        error_code="package_validation_failed",
        error_summary=exc,
    )


def _build_identity_resolution_failure_entry(item: ContentItem, resolution) -> dict:
    return build_runlog_entry(
        input_url=item.url,
        platform=item.platform,
        backend=str(resolution.provider or "identity.not_available"),
        status="failed",
        source_type=item.source_type,
        source_level="partial_extraction",
        error_code=resolution.error_code or "identity_resolution_failed",
        error_summary=resolution.error_summary or "Durable item identity resolution failed.",
        resume_pointer="rerun after the identity resolver is available; unresolved identity was not skipped or extracted",
    )


def _build_package_validation_failure_entry_for_item(
    item: ContentItem,
    exc: PackageValidationError,
) -> dict:
    error_code = str(exc)
    if error_code not in {"execution_fingerprint_missing", "execution_fingerprint_mismatch"}:
        error_code = "package_validation_failed"
    return build_runlog_entry(
        input_url=item.url,
        platform=item.platform,
        backend="existing_package",
        status="failed",
        source_type=item.source_type,
        source_level="partial_extraction",
        error_code=error_code,
        error_summary=exc,
    )


def _build_skipped_package_runlog_entry(
    item: ContentItem,
    output: OutputWriteResult,
) -> dict:
    metadata = _read_package_metadata(output.path)
    backend = metadata.get("backend")
    backend_name = "existing_package"
    if isinstance(backend, dict) and backend.get("name"):
        backend_name = str(backend["name"])
    elif isinstance(backend, str) and backend.strip():
        backend_name = backend
    return build_runlog_entry(
        input_url=item.url,
        platform=item.platform,
        backend=backend_name,
        status=str(metadata.get("status") or "verified"),
        source_type=item.source_type,
        source_level=str(metadata.get("source_level") or "partial_extraction"),
        output_path=output.path,
        package_id=output.package_id,
        package_operation=output.package_operation,
    )


def _read_package_metadata(package_path: Path) -> dict:
    try:
        value = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def cmd_doctor(args: argparse.Namespace) -> int:
    profile = getattr(args, "profile", "core")
    payload = {
        "ok": True,
        "command": "doctor",
        "official_command": "openmd",
        "module_entry": "python -m openmd",
        "repo_wrapper": str(Path("scripts/openmd.ps1")),
        "openmd_on_path": shutil.which("openmd") is not None,
        "safe_session_boundary": {
            "core_owns_platform_sessions": False,
            "reads_private_browser_profiles": False,
            "imports_or_exports_platform_sessions": False,
            "default_raw_artifact_retention": False,
        },
        "structured_states": [
            "auth_required",
            "session_expired",
            "permission_required",
            "cache_stale",
            "refresh_needed",
            "no_delta",
            "sidecar_unreachable",
        ],
        "next_action": (
            "Use platform or sidecar UI to repair login/session/cache state, "
            "then rerun Openmd. Core records status only and does not store private session material."
        ),
        "profile": profile,
        "profile_diagnostics": build_doctor_report(profile),
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    records = filter_capabilities(source_id=args.source)
    if args.source and not records:
        print(json.dumps({"count": 0, "capabilities": []}, ensure_ascii=True, sort_keys=True))
        return 1
    if args.format == "json":
        print(json.dumps({"count": len(records), "capabilities": [record.as_dict() for record in records]}, ensure_ascii=True, sort_keys=True))
        return 0
    print("source_id\ttier\tstate\tplanned_provider\truntime_provider\tbinding_state\texecution_ready")
    for record in records:
        print("\t".join((record.source_id, record.tier, record.state, record.planned_provider or "", record.runtime_provider or "", record.binding_state, str(record.execution_ready).lower())))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    result = build_plan(
        args.input,
        input_kind=args.input_kind,
        request_all=args.all,
        available_gates=set(args.gate),
    )
    payload = result.as_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}\t{value}")
    return 0 if result.ok else 1


def cmd_report_weixin_author_batch(args: argparse.Namespace) -> int:
    from openmd.adapters.weixin.report import load_runlog_entries, write_weixin_author_batch_report

    write_json = args.format in {"both", "json"}
    write_markdown = args.format in {"both", "markdown"}
    entries = load_runlog_entries(args.runlog)
    result = write_weixin_author_batch_report(
        entries,
        args.out,
        write_json=write_json,
        write_markdown=write_markdown,
    )
    outputs = [str(path) for path in (result.json_path, result.markdown_path) if path is not None]
    summary = result.report.get("summary", {})
    _print_result(
        CliResult(
            ok=True,
            command="report",
            item_count=int(summary.get("total_items", 0)),
            success_count=int(summary.get("covered_count", 0)),
            failed_count=int(summary.get("failed_count", 0)),
            outputs=outputs,
            message="report written",
        )
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    start_error = _start_compact_receipt(args, "ingest")
    if start_error:
        return _emit_result(args, CliResult(ok=False, command="ingest", failed_count=1, message=start_error))
    try:
        args.url = _read_single_input(args.url)
    except ValueError as exc:
        result = CliResult(ok=False, command="ingest", failed_count=1, message=sanitize_error_summary(exc))
        if getattr(args, "receipt", None) == "compact":
            return _emit_result(args, result)
        print(f"openmd ingest: {sanitize_error_summary(exc)}", file=sys.stderr)
        return 1
    result = _process_input(args.url, args, command="ingest")
    return _emit_result(args, result)


def cmd_batch(args: argparse.Namespace) -> int:
    start_error = _start_compact_receipt(args, "batch")
    if start_error:
        return _emit_result(args, CliResult(ok=False, command="batch", failed_count=1, message=start_error))
    try:
        urls = _read_urls_file(args.urls_file)
    except OSError as exc:
        result = CliResult(ok=False, command="batch", failed_count=1, message=sanitize_error_summary(exc))
        if getattr(args, "receipt", None) == "compact":
            return _emit_result(args, result)
        print(f"openmd batch: {sanitize_error_summary(exc)}", file=sys.stderr)
        return 1

    _append_large_batch_guardrail_if_needed(args, command="batch", total=len(urls))

    urls_to_process = urls
    if getattr(args, "receipt", None) == "compact":
        occurrences: dict[str, int] = {}
        unique_urls: list[str] = []
        for url in urls:
            identity = sanitize_url(url)
            occurrences[identity] = occurrences.get(identity, 0) + 1
            if occurrences[identity] == 1:
                unique_urls.append(url)
        args._compact_input_occurrences_by_url = occurrences
        urls_to_process = unique_urls
        _register_compact_inputs(args, {
            _compact_url_identity(url): occurrences[sanitize_url(url)]
            for url in unique_urls
        })

    aggregate = CliResult(ok=True, command="batch", message="completed")
    consecutive_failures = 0
    for line_number, url in enumerate(urls_to_process, start=1):
        result = _process_input(url, args, command="batch")
        _sync_compact_rows(args)
        if getattr(args, "receipt", None) == "compact":
            args._compact_pending_identities.pop(_compact_url_identity(url), None)
        aggregate.item_count += result.item_count
        aggregate.success_count += result.success_count
        aggregate.failed_count += result.failed_count
        aggregate.outputs.extend(result.outputs)
        for signal in result.guardrail_signals:
            if signal not in aggregate.guardrail_signals:
                aggregate.guardrail_signals.append(signal)
        if not result.ok:
            aggregate.ok = False
            aggregate.message = "completed with failures"
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if _guardrail_failure_streak_reached(args, consecutive_failures):
            _append_guardrail_stop_entry(args, command="batch", index=line_number, total=len(urls))
            if "consecutive_failure_stop" not in aggregate.guardrail_signals:
                aggregate.guardrail_signals.append("consecutive_failure_stop")
            aggregate.message = "guardrail stopped"
            break
    return _emit_result(args, aggregate)


def _write_youtube_discovery_summary(out_dir: str, discovery_result, **fields: object) -> Path:
    summary = {
        "page_count": discovery_result.metadata.get("page_count", 0),
        "request_count": discovery_result.metadata.get("request_count", 0),
        **fields,
    }
    summary_path = Path(out_dir) / "youtube-discovery-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary_path


def cmd_collect(args: argparse.Namespace) -> int:
    start_error = _start_compact_receipt(args, "collect")
    if start_error:
        return _emit_result(args, CliResult(ok=False, command="collect", failed_count=1, message=start_error), discovery_complete=False)
    try:
        args.url = _read_single_input(args.url)
    except ValueError as exc:
        result = CliResult(ok=False, command="collect", failed_count=1, message=sanitize_error_summary(exc))
        if getattr(args, "receipt", None) == "compact":
            return _emit_result(args, result, discovery_complete=False)
        print(f"openmd collect: {sanitize_error_summary(exc)}", file=sys.stderr)
        return 1
    if args.all and not _allow_scoped_all_collect(args.url, args):
        result = _log_all_guard(args.url, args, command="collect")
        return _emit_result(args, result, discovery_complete=False)

    platform = _detect_collect_platform(args.url)
    _select_collect_discovery_backend(args.url, args)
    discovery_executor = _build_discovery_executor(args, platform=platform)
    discovery_result = discovery_executor.discover(
        DiscoveryRequest(
            input_url=args.url,
            platform=platform,
            limit=None if args.all else args.limit,
            allow_all=bool(args.all),
        )
    )

    if not discovery_result.ok:
        partial_outputs: list[str] = []
        partial_count = 0
        if platform == "youtube":
            # Failed discovery must preserve known inputs without extracting or
            # claiming them verified, including compact expected-item accounting.
            _prepare_compact_collection(args, discovery_result.items)
            args._compact_execution_failed = True
            partial_count = len(discovery_result.items)
            partial_manifest = write_discovery_manifest(
                out_dir=args.out, platform=platform,
                collection_url=str(discovery_result.metadata.get("normalized_url") or args.url),
                provider=discovery_result.provider, items=discovery_result.items,
                limit=None if args.all else args.limit,
            )
            partial_outputs.append(str(partial_manifest))
            summary_path = _write_youtube_discovery_summary(
                args.out, discovery_result,
                status="failed", source_exhausted=False,
                item_count=partial_count, error_code=discovery_result.error_code,
                stop_reason=discovery_result.error_code,
                resume_policy="restart_discovery_and_reuse_verified_packages",
            )
            partial_outputs.append(str(summary_path))
        entry = build_runlog_entry(
            input_url=args.url,
            platform=platform,
            backend=discovery_result.provider,
            status="failed",
            source_type="mixed",
            source_level="partial_extraction",
            item_count=partial_count,
            success_count=0,
            failed_count=1,
            error_code=discovery_result.error_code,
            error_summary=discovery_result.error_summary,
            event_type="discovery" if platform == "youtube" else "item",
        )
        _append_execution_entry(args, entry)
        result = CliResult(
            ok=False,
            command="collect",
            item_count=partial_count,
            success_count=0,
            failed_count=1,
            outputs=partial_outputs,
            message=discovery_result.error_summary or "collection discovery failed",
        )
        return _emit_result(args, result, discovery_complete=False)

    discovered_items = _prepare_compact_collection(args, discovery_result.items)
    discovery_complete = not args.all or bool(discovery_result.metadata.get("source_exhausted"))
    collection_url = str(discovery_result.metadata.get("normalized_url") or args.url)
    manifest_path = write_discovery_manifest(
        out_dir=args.out,
        platform=platform,
        collection_url=collection_url,
        provider=discovery_result.provider,
        items=discovered_items,
        limit=None if args.all else args.limit,
    )
    _append_large_batch_guardrail_if_needed(args, command="collect", total=len(discovery_result.items))
    if platform == "youtube" and discovery_result.provider.startswith("youtube.data-api"):
        metadata = discovery_result.metadata
        _write_youtube_discovery_summary(
            args.out, discovery_result,
            status="ok", source_exhausted=bool(metadata.get("source_exhausted")),
            item_count=len(discovery_result.items),
            playlist_item_count=metadata.get("playlist_item_count", 0),
            duplicate_item_count=metadata.get("duplicate_item_count", 0),
            stop_reason=metadata.get("stop_reason"),
        )

    if getattr(args, "manifest_only", False):
        entry = build_runlog_entry(
            input_url=collection_url,
            platform=platform,
            backend=discovery_result.provider,
            status="verified",
            source_type="mixed",
            source_level="third_party_extractor",
            output_path=manifest_path,
            item_count=len(discovery_result.items),
            success_count=len(discovery_result.items),
            failed_count=0,
            event_type="discovery",
        )
        _append_execution_entry(args, entry)
        result = CliResult(
            ok=bool(discovery_result.items) or (platform == "youtube" and args.all and discovery_complete),
            command="collect",
            item_count=len(discovery_result.items),
            success_count=len(discovery_result.items),
            failed_count=0,
            outputs=[str(manifest_path)],
            message="manifest written",
        )
        return _emit_result(args, result, discovery_complete=discovery_complete, manifest_only=True)

    start_index = max(0, int(getattr(args, "start_index", 0) or 0))
    max_items = max(0, int(getattr(args, "max_items", 0) or 0))
    remaining_items = discovered_items[start_index:]
    extraction_items = remaining_items[:max_items] if max_items else remaining_items
    result = _process_items(extraction_items, args, command="collect")
    if platform == "douyin" and args.all:
        coverage_path = _write_douyin_coverage_summary(
            args=args,
            result=result,
            manifest_path=manifest_path,
            discovered_count=len(discovery_result.items),
            source_exhausted=bool(discovery_result.metadata.get("source_exhausted")),
            discovery_metadata=dict(discovery_result.metadata),
            start_index=start_index,
            max_items=max_items,
        )
        result.outputs.append(str(coverage_path))
    elif platform == "xiaohongshu" and args.all:
        coverage_path = _write_xiaohongshu_coverage_summary(
            args=args,
            result=result,
            discovered_items=discovery_result.items,
            discovery_metadata=dict(discovery_result.metadata),
            start_index=start_index,
            max_items=max_items,
        )
        result.outputs.append(str(coverage_path))
    return _emit_result(
        args,
        result,
        discovery_complete=discovery_complete,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
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
        return args.func(args)
    except (Exception, KeyboardInterrupt) as exc:
        if getattr(args, "receipt", None) == "compact":
            args._compact_execution_failed = not isinstance(exc, KeyboardInterrupt)
            if getattr(args, "_compact_receipt_run", None) is not None:
                return _emit_result(args, CliResult(ok=False, command=args.command, message="execution_interrupted"), discovery_complete=False)
            return _print_compact_failure()
        print(f"openmd: {sanitize_error_summary(exc)}", file=sys.stderr)
        return 1
    finally:
        lock = getattr(args, "_compact_output_lock", None)
        if lock is not None:
            try:
                lock.release()
            except OSError:
                pass  # The bounded failure path already reports failure; leave residual lock for inspection.


def _write_douyin_coverage_summary(
    *,
    args: argparse.Namespace,
    result: CliResult,
    manifest_path: Path,
    discovered_count: int,
    source_exhausted: bool,
    discovery_metadata: dict[str, object],
    start_index: int,
    max_items: int,
) -> Path:
    path = Path(args.out) / "coverage-summary.json"
    payload = {
        "platform": "douyin",
        "creator_input": args.url,
        "discovery_provider": getattr(args, "discovery_backend", None),
        "transcript_backend": getattr(args, "backend", None),
        "source_exhausted": source_exhausted,
        "page_count": discovery_metadata.get("page_count"),
        "empty_page_count": discovery_metadata.get("empty_page_count"),
        "duplicate_page_count": discovery_metadata.get("duplicate_page_count"),
        "pagination_retry_count": discovery_metadata.get("pagination_retry_count"),
        "video_count": discovery_metadata.get("video_count"),
        "image_post_count": discovery_metadata.get("image_post_count"),
        "discovered": discovered_count,
        "attempted": result.item_count,
        "start_index": start_index,
        "slice_size": max_items,
        "success": result.success_count,
        "failed": result.failed_count,
        "skipped_existing": result.skipped_count,
        "remaining_unattempted": max(0, discovered_count - start_index - result.item_count),
        "complete_run": start_index == 0 and max_items == 0 and result.item_count == discovered_count,
        "source_level_counts": _package_coverage_source_level_counts(result.source_level_counts),
        "failure_code_counts": result.failure_code_counts,
        "guardrail_signals": result.guardrail_signals,
        "manifest": str(manifest_path),
        "runlog": str(_runlog_path(args)),
        "media_retention": "none",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _package_coverage_source_level_counts(counts: dict[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for source_level, count in counts.items():
        key = package_source_level(source_level)
        normalized[key] = normalized.get(key, 0) + int(count)
    return normalized


def _write_xiaohongshu_coverage_summary(
    *,
    args: argparse.Namespace,
    result: CliResult,
    discovered_items: list[ContentItem],
    discovery_metadata: dict[str, object],
    start_index: int,
    max_items: int,
) -> Path:
    """Write a path-free, count-only Xiaohongshu full-collection summary."""
    path = Path(args.out) / "coverage-summary.json"
    discovered = len(discovered_items)
    remaining_unattempted = max(0, discovered - start_index - result.item_count)
    coverage = dict(result.coverage)
    retryable_items = _non_negative_int(coverage.get("retryable_items"))
    terminal_gaps = _non_negative_int(coverage.get("terminal_gaps"))
    remaining = remaining_unattempted + retryable_items
    component_counts, component_source_counts = _xiaohongshu_component_coverage(coverage)

    payload = {
        "platform": "xiaohongshu",
        "package_mode": bool(getattr(args, "package", False)),
        "discovered": discovered,
        **collection_content_type_counts(discovered_items),
        "attempted": result.item_count,
        "verified": _non_negative_int(coverage.get("verified")),
        "partial": _non_negative_int(coverage.get("partial")),
        "terminal_gaps": terminal_gaps,
        "terminal_gap_items": _non_negative_int(coverage.get("terminal_gap_items")),
        "retryable": _non_negative_int(coverage.get("retryable")),
        "retryable_items": retryable_items,
        "failed": result.failed_count,
        "skipped_existing": result.skipped_count,
        "remaining": remaining,
        "remaining_unattempted": remaining_unattempted,
        "start_index": start_index,
        "slice_size": max_items,
        "complete_run": start_index == 0 and max_items == 0 and result.item_count == discovered,
        "source_exhausted": discovery_metadata.get("source_exhausted") is True,
        "explicit_exhaustion": discovery_metadata.get("explicit_exhaustion") is True,
        "page_count": _optional_non_negative_int(discovery_metadata.get("page_count")),
        "scroll_count": _optional_non_negative_int(discovery_metadata.get("scroll_count")),
        "anomaly_count": _optional_non_negative_int(discovery_metadata.get("anomaly_count")),
        "stable_round_count": _optional_non_negative_int(discovery_metadata.get("stable_round_count")),
        "empty_snapshot_count": _optional_non_negative_int(discovery_metadata.get("empty_snapshot_count")),
        "duplicate_snapshot_count": _optional_non_negative_int(
            discovery_metadata.get("duplicate_snapshot_count")
        ),
        "component_counts": component_counts,
        "component_source_counts": component_source_counts,
        "source_counts": _safe_count_mapping(coverage.get("source_counts")),
        "failure_code_counts": _safe_count_mapping(result.failure_code_counts),
        "guardrail_signals": list(result.guardrail_signals),
        "media_retention": "none",
    }
    payload["outcome"] = (
        "failed"
        if result.failed_count or remaining
        else "complete_with_gaps"
        if terminal_gaps
        else "verified"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _xiaohongshu_component_coverage(
    coverage: dict[str, object],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    raw_counts = coverage.get("component_counts")
    raw_sources = coverage.get("component_source_counts")
    count_mapping = raw_counts if isinstance(raw_counts, dict) else {}
    source_mapping = raw_sources if isinstance(raw_sources, dict) else {}
    component_counts: dict[str, dict[str, int]] = {}
    component_source_counts: dict[str, dict[str, int]] = {}
    for component_id in ("native_text", "image_ocr", "video_transcript"):
        component_counts[component_id] = _safe_count_mapping(count_mapping.get(component_id))
        component_counts[component_id].setdefault("total", 0)
        component_source_counts[component_id] = _safe_count_mapping(source_mapping.get(component_id))
    return component_counts, component_source_counts


def _safe_count_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _non_negative_int(count)
        for key, count in value.items()
        if isinstance(key, str)
    }


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _guardrail_item_threshold(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "guardrail_item_threshold", DEFAULT_GUARDRAIL_ITEM_THRESHOLD) or 0))


def _guardrail_failure_streak(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "guardrail_failure_streak", DEFAULT_GUARDRAIL_FAILURE_STREAK) or 0))


def _guardrail_failure_streak_reached(args: argparse.Namespace, consecutive_failures: int) -> bool:
    threshold = _guardrail_failure_streak(args)
    return threshold > 0 and consecutive_failures >= threshold


def _append_large_batch_guardrail_if_needed(
    args: argparse.Namespace,
    *,
    command: str,
    total: int,
) -> None:
    threshold = _guardrail_item_threshold(args)
    if threshold <= 0 or total < threshold:
        return
    append_runlog_entry(
        _runlog_path(args),
        build_runlog_entry(
            input_url=getattr(args, "urls_file", None) or getattr(args, "url", "") or "",
            platform=_detect_collect_platform(getattr(args, "url", "")) if command == "collect" else "mixed",
            backend="openmd.batch.guardrail",
            status="partial",
            source_type="mixed",
            source_level="partial_extraction",
            item_count=total,
            success_count=0,
            failed_count=0,
            event_type="guardrail",
            guardrail_signals=["large_item_count"],
            resume_pointer="runlog records per-item progress; rerun uses skip-existing/package dedupe where applicable",
        ),
    )


def _append_guardrail_stop_entry(
    args: argparse.Namespace,
    *,
    command: str,
    index: int,
    total: int,
) -> None:
    append_runlog_entry(
        _runlog_path(args),
        build_runlog_entry(
            input_url=getattr(args, "urls_file", None) or getattr(args, "url", "") or "",
            platform=_detect_collect_platform(getattr(args, "url", "")) if command == "collect" else "mixed",
            backend="openmd.batch.guardrail",
            status="failed",
            source_type="mixed",
            source_level="partial_extraction",
            item_count=total,
            success_count=0,
            failed_count=0,
            event_type="guardrail",
            failure_class="runtime_transient",
            guardrail_signals=["consecutive_failure_stop"],
            resume_pointer=f"resume from item {index + 1} of {total}",
            error_code="guardrail_consecutive_failures",
            error_summary=(
                "Batch paused after consecutive failures. Inspect recent runlog rows, "
                "fix backend/runtime state, then resume from the recorded pointer."
            ),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
