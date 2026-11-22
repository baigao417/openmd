"""CLI collections responsibility; compatibility imports resolve at call time."""
from __future__ import annotations

def _write_youtube_discovery_summary(out_dir: str, discovery_result, **fields: object) -> Path:
    from .cli import Path, json

    summary = {
        "page_count": discovery_result.metadata.get("page_count", 0),
        "request_count": discovery_result.metadata.get("request_count", 0),
        **fields,
    }
    summary_path = Path(out_dir) / "youtube-discovery-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary_path


def cmd_collect(args: argparse.Namespace) -> int:
    from .cli import CliResult, DiscoveryRequest, _allow_scoped_all_collect, _append_execution_entry, _append_large_batch_guardrail_if_needed, _build_discovery_executor, _detect_collect_platform, _emit_result, _log_all_guard, _prepare_compact_collection, _process_items, _read_single_input, _select_collect_discovery_backend, _start_compact_receipt, _write_douyin_coverage_summary, _write_xiaohongshu_coverage_summary, _write_youtube_discovery_summary, argparse, build_runlog_entry, sanitize_error_summary, sys, write_discovery_manifest

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
    from openmd.core.input_validation import normalize_http_url

    args.url = normalize_http_url(args.url)
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

    from dataclasses import replace

    discovered_items = [
        replace(item, url=normalize_http_url(item.url))
        for item in discovery_result.items
    ]

    if not discovery_result.ok:
        partial_outputs: list[str] = []
        partial_count = 0
        if platform == "youtube":
            # Failed discovery must preserve known inputs without extracting or
            # claiming them verified, including compact expected-item accounting.
            _prepare_compact_collection(args, discovered_items)
            args._compact_execution_failed = True
            partial_count = len(discovered_items)
            partial_manifest = write_discovery_manifest(
                out_dir=args.out, platform=platform,
                collection_url=normalize_http_url(str(discovery_result.metadata.get("normalized_url") or args.url)),
                provider=discovery_result.provider, items=discovered_items,
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

    discovered_items = _prepare_compact_collection(args, discovered_items)
    discovery_complete = not args.all or bool(discovery_result.metadata.get("source_exhausted"))
    collection_url = normalize_http_url(str(discovery_result.metadata.get("normalized_url") or args.url))
    manifest_path = write_discovery_manifest(
        out_dir=args.out,
        platform=platform,
        collection_url=collection_url,
        provider=discovery_result.provider,
        items=discovered_items,
        limit=None if args.all else args.limit,
    )
    _append_large_batch_guardrail_if_needed(args, command="collect", total=len(discovered_items))
    if platform == "youtube" and discovery_result.provider.startswith("youtube.data-api"):
        metadata = discovery_result.metadata
        _write_youtube_discovery_summary(
            args.out, discovery_result,
            status="ok", source_exhausted=bool(metadata.get("source_exhausted")),
            item_count=len(discovered_items),
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
            item_count=len(discovered_items),
            success_count=len(discovered_items),
            failed_count=0,
            event_type="discovery",
        )
        _append_execution_entry(args, entry)
        result = CliResult(
            ok=bool(discovered_items) or (platform == "youtube" and args.all and discovery_complete),
            command="collect",
            item_count=len(discovered_items),
            success_count=len(discovered_items),
            failed_count=0,
            outputs=[str(manifest_path)],
            message="manifest written",
        )
        return _emit_result(args, result, discovery_complete=discovery_complete and result.ok, manifest_only=True)

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
            discovered_count=len(discovered_items),
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
            discovered_items=discovered_items,
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
    from .cli import CliResult, Path, _package_coverage_source_level_counts, _runlog_path, argparse, json

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
    from .cli import package_source_level

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
    from .cli import CliResult, ContentItem, Path, _non_negative_int, _optional_non_negative_int, _safe_count_mapping, _xiaohongshu_component_coverage, argparse, collection_content_type_counts, json

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
    from .cli import _safe_count_mapping

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
    from .cli import _non_negative_int

    if not isinstance(value, dict):
        return {}
    return {
        str(key): _non_negative_int(count)
        for key, count in value.items()
        if isinstance(key, str)
    }


def _optional_non_negative_int(value: object) -> int | None:
    from .cli import _non_negative_int

    if value is None:
        return None
    return _non_negative_int(value)


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _guardrail_item_threshold(args: argparse.Namespace) -> int:
    from .cli import DEFAULT_GUARDRAIL_ITEM_THRESHOLD, argparse

    return max(0, int(getattr(args, "guardrail_item_threshold", DEFAULT_GUARDRAIL_ITEM_THRESHOLD) or 0))


def _guardrail_failure_streak(args: argparse.Namespace) -> int:
    from .cli import DEFAULT_GUARDRAIL_FAILURE_STREAK, argparse

    return max(0, int(getattr(args, "guardrail_failure_streak", DEFAULT_GUARDRAIL_FAILURE_STREAK) or 0))


def _guardrail_failure_streak_reached(args: argparse.Namespace, consecutive_failures: int) -> bool:
    from .cli import _guardrail_failure_streak, argparse

    threshold = _guardrail_failure_streak(args)
    return threshold > 0 and consecutive_failures >= threshold


def _append_large_batch_guardrail_if_needed(
    args: argparse.Namespace,
    *,
    command: str,
    total: int,
) -> None:
    from .cli import _detect_collect_platform, _guardrail_item_threshold, _runlog_path, append_runlog_entry, argparse, build_runlog_entry

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
    from .cli import _detect_collect_platform, _runlog_path, append_runlog_entry, argparse, build_runlog_entry

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
