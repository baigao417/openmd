"""CLI receipts responsibility; compatibility imports resolve at call time."""
from __future__ import annotations

def _start_compact_receipt(args: argparse.Namespace, command: str) -> str | None:
    from .cli import CompactOutputLock, CompactReceiptRun, _runlog_path, argparse

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
    from .cli import CompactReceiptRun

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
    from .cli import _compact_failure_payload, json

    print(json.dumps(_compact_failure_payload(), ensure_ascii=True, separators=(",", ":")))
    return 1


def _register_compact_inputs(args: argparse.Namespace, identities: dict[str, int]) -> None:
    from .cli import argparse

    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        return
    args._compact_inputs_registered = True
    args._compact_pending_identities = identities
    run.replace_expected(identities)


def _sync_compact_rows(args: argparse.Namespace) -> None:
    from .cli import _read_new_runlog_rows, _record_compact_rows, argparse

    run = getattr(args, "_compact_receipt_run", None)
    if run is None:
        return
    path = args._compact_runlog_path
    rows = _read_new_runlog_rows(path, args._compact_runlog_offset)
    _record_compact_rows(run, rows, args.out, args=args)
    args._compact_runlog_offset = path.stat().st_size if path.exists() else 0


def _append_execution_entry(args: argparse.Namespace, entry: dict[str, object]) -> None:
    from .cli import _runlog_path, _sync_compact_rows, append_runlog_entry, argparse

    append_runlog_entry(_runlog_path(args), entry)
    # Receipt/checkpoint must be durable before the next provider call.
    _sync_compact_rows(args)


def _prepare_compact_collection(args: argparse.Namespace, items: list[ContentItem]) -> list[ContentItem]:
    from .cli import ContentItem, _compact_item_identity, _register_compact_inputs, argparse, sanitize_url

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
    from .cli import ContentItem, _compact_item_identity, argparse, sanitize_url

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
    from .cli import ContentItem, _compact_item_identity, _compact_url_identity, argparse, sanitize_url

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
    from .cli import CliResult, CompactReceiptRun, _print_compact_failure, _print_result, _sync_compact_rows, argparse, json

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
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return exit_code
    except (OSError, ValueError):
        return _print_compact_failure()


def _read_new_runlog_rows(path: Path, offset: int) -> list[dict[str, object]]:
    from .cli import Path, json

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
    from .cli import CompactReceiptRun, ContentItem, sanitize_url

    return CompactReceiptRun.stable_identity(item.platform, sanitize_url(item.url))


def _compact_url_identity(url: str) -> str:
    from .cli import CompactReceiptRun, detect_platform, sanitize_url

    return CompactReceiptRun.stable_identity(detect_platform(url), sanitize_url(url))


def _prepare_resolved_item(
    args: argparse.Namespace,
    original: ContentItem,
    resolved: ContentItem,
    provider_chain: tuple[str, ...],
) -> tuple[ContentItem, str | None] | None:
    from .cli import ContentItem, _compact_execution_fingerprint, _compact_item_identity, _reconcile_compact_identity, apply_platform_extraction_contract, argparse, replace

    """Return the contract-applied item and its fingerprint, or None if the receipt already has it."""
    item = apply_platform_extraction_contract(resolved)
    if item.platform == "douyin" and getattr(args, "backend", None) == "douyin.metadata":
        item = replace(item, raw_meta={**item.raw_meta, "extraction_target": "metadata"})
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
    from .cli import CompactReceiptRun, Path, _compact_terminal_status, argparse, sanitize_url

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
            route_trace=row.get("route_trace") if isinstance(row.get("route_trace"), dict) else None,
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
    from .cli import ContentItem, argparse, execution_fingerprint, extraction_contract_version_for, required_component_ids_for_item

    if getattr(args, "receipt", None) != "compact" or not getattr(args, "package", False):
        return None
    raw_meta = item.raw_meta if isinstance(item.raw_meta, dict) else {}
    modalities_value = raw_meta.get("modalities")
    modalities = tuple(str(value) for value in modalities_value) if isinstance(modalities_value, list) else (item.source_type,)
    effective_config: dict[str, object] = {}
    if item.platform == 'xiaoe':
        from openmd.core.local_config import load_local_config
        effective_config['xiaoe_long_asr'] = load_local_config().get('xiaoe_long_asr', {'enabled': False})
        effective_config['explicit_cloud_gate'] = bool(getattr(args, 'enable_cloud_backends', False))
    if item.platform in {"pdf", "screenshot"} and any(backend.startswith(("ocr.", "pdf.")) for backend in provider_chain):
        from openmd.ocr.config import settings

        effective_config["ocr_router"] = settings()
    from openmd.runtime.asr import backend_execution_config

    if "asr.faster_whisper" in provider_chain:
        effective_config["faster_whisper"] = backend_execution_config("asr.faster_whisper")
    from openmd.routing.loader import load_catalog

    composed_asr = any(load_catalog().capabilities.get(backend, {}).get("then") == "asr.auto" for backend in provider_chain)
    if "asr.auto" in provider_chain or composed_asr or "douyin.transcript" in provider_chain:
        effective_config["asr_auto"] = backend_execution_config("asr.auto")
    if "asr.qwen3" in provider_chain:
        effective_config["qwen3"] = backend_execution_config("asr.qwen3")
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
