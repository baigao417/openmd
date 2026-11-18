"""CLI items responsibility; compatibility imports resolve at call time."""
from __future__ import annotations

def _process_items(
    items: list[ContentItem],
    args: argparse.Namespace,
    *,
    command: str,
) -> CliResult:
    from .cli import BackendExecutor, CliResult, CollectionCoverage, ContentItem, OutputWriteResult, PackageValidationError, _append_execution_entry, _append_guardrail_stop_entry, _build_backend_executor, _build_identity_resolution_failure_entry, _build_package_validation_failure_entry, _build_package_validation_failure_entry_for_item, _build_skipped_package_runlog_entry, _guardrail_failure_streak_reached, _maybe_skip_existing_package, _pace_douyin_extraction, _prepare_resolved_item, _read_package_metadata, _requested_backend, _write_extraction_output, argparse, build_item_runlog_entry, create_default_registry, json, sys

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
            _requested_backend(item.platform, args),
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

        _pace_douyin_extraction(item, args)
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
    from .cli import CliResult, OutputWriteResult, PackageValidationError, Path, _append_execution_entry, _build_backend_executor, _build_identity_resolution_failure_entry, _build_package_validation_failure_entry, _build_package_validation_failure_entry_for_item, _build_skipped_package_runlog_entry, _log_all_guard, _maybe_skip_existing_package, _pace_douyin_extraction, _prepare_compact_collection, _prepare_resolved_item, _register_resolved_batch_items, _requested_backend, _write_extraction_output, apply_platform_extraction_contract, argparse, build_item_runlog_entry, create_default_registry

    if getattr(args, "all", False):
        return _log_all_guard(input_url, args, command=command)

    registry = create_default_registry()
    if getattr(args, "as_snapshot", False):
        from openmd.core.models import ContentItem

        snapshot_path = Path(input_url).expanduser().resolve()
        original_url = str(getattr(args, "snapshot_url", None) or "https://snapshot.invalid/local")
        item = ContentItem(
            platform="page_snapshot",
            input_type="single",
            url=original_url,
            title=None,
            source_type="article",
            raw_meta={"snapshot_path": str(snapshot_path), "captured_from": "browser_snapshot", "coverage": "visible_branch"},
        )
        adapter = None
        selection = registry.select_backend("page_snapshot", _requested_backend("page_snapshot", args), allow_fallback=args.fallback)
        items = [item]
    else:
        adapter = registry.adapter_for(input_url)
        selection = registry.select_backend(
            adapter.id,
            _requested_backend(adapter.id, args),
            allow_fallback=args.fallback,
        )
        items = [
            apply_platform_extraction_contract(item)
            for item in adapter.resolve_items(input_url, limit=args.limit, all=False)
        ]
    if getattr(args, "enable_network_backends", False) and any(b.startswith("opencli.") for b in selection.chain):
        from openmd.runtime.managed_tools import ensure_opencli

        # One `pip install openmd-cli` is enough: fetch the pinned opencli on first need.
        ensure_opencli()
    if command == "ingest":
        items = _prepare_compact_collection(args, items)
    elif command == "batch":
        _register_resolved_batch_items(args, input_url, items)
    executor = _build_backend_executor(args, platform="page_snapshot" if getattr(args, "as_snapshot", False) else adapter.id)
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

        _pace_douyin_extraction(item, args)
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
    from .cli import ContentItem, OutputWriteResult, PackageValidationError, _read_package_metadata, argparse, find_corrupt_package_for_item, find_existing_package_for_item, package_metadata_satisfies_extraction_contract, require_matching_fingerprint, validate_package_or_raise

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
    from .cli import ContentItem, OutputWriteResult, PackageValidationError, argparse, shutil, validate_package_or_raise, write_item_package, write_item_result

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
            raw=getattr(args, "raw", False),
        )
    )


def _build_package_validation_failure_entry(
    item: ContentItem,
    result,
    exc: PackageValidationError,
) -> dict:
    from .cli import ContentItem, PackageValidationError, build_runlog_entry

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
    from .cli import ContentItem, build_runlog_entry

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
    from .cli import ContentItem, PackageValidationError, build_runlog_entry

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
    from .cli import ContentItem, OutputWriteResult, _read_package_metadata, build_runlog_entry

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
    from .cli import Path, json

    try:
        value = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
