"""CLI commands responsibility; compatibility imports resolve at call time."""
from __future__ import annotations


def cmd_xiaoyuzhou_auth(args) -> int:
    import sys
    from openmd.platforms.xiaoyuzhou_auth import AuthError, CredentialStore, begin_login
    from openmd.interactive import is_interactive

    store = CredentialStore()
    if args.auth_action == "logout":
        store.delete()
        store.pending_path.unlink(missing_ok=True)
        print("小宇宙：未登录")
        return 0
    interactive = is_interactive()
    try:
        result = begin_login(store, wait=interactive, terminal=interactive)
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        print("请检查网络后重试 openmd login xiaoyuzhou。", file=sys.stderr)
        return 1
    if result.status == "logged_in":
        print("小宇宙：已登录")
        return 0
    if result.qr_path and not interactive:
        print(f"二维码：{result.qr_path}\n请用小宇宙 App 扫码；完成后重试 openmd login xiaoyuzhou。")
        return 0
    print("小宇宙：需要重新扫码。运行 openmd login xiaoyuzhou 重试。", file=sys.stderr)
    return 1


def cmd_opencli(args) -> int:
    import argparse
    import json
    import sys
    from pathlib import Path
    from openmd.runtime import managed_tools

    argv = list(args.opencli_args)
    if argv[:1] == ["migrate"]:
        parser = argparse.ArgumentParser(prog="openmd opencli migrate")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--to", type=Path)
        options = parser.parse_args(argv[1:])
        if options.apply and options.dry_run:
            parser.error("--apply and --dry-run are mutually exclusive")
        print(json.dumps(managed_tools.migrate_config(destination=options.to, apply=options.apply), ensure_ascii=True))
        return 0
    allow_write = getattr(args, "allow_write", False) or "--allow-write" in argv
    argv = [a for a in argv if a != "--allow-write"]
    if not argv:
        argv = ["list"]
    if managed_tools.ensure_opencli() != "managed":
        print("managed_opencli_missing", file=sys.stderr)
        return 127
    from openmd.runtime.opencli_adapters import prepare_bundled_plugins
    prepare_bundled_plugins()
    access = managed_tools.command_access(argv)
    if access != "read" and not allow_write:
        print("opencli_write_not_allowed: command access is write or unknown; use --allow-write", file=sys.stderr)
        return 2
    return managed_tools.passthrough(argv)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .cli import Path, argparse, build_doctor_report, json, sanitize_error_summary, shutil, sys

    from openmd.runtime.managed_tools import status as managed_tools_status

    if getattr(args, "clear_gpu_quarantine", False):
        from openmd.runtime.gpu_quarantine import clear_quarantine_manually

        print(json.dumps({"command": "doctor", "ok": True, "gpu_quarantine": clear_quarantine_manually()}, sort_keys=True))
        return 0
    from openmd.runtime.tempdirs import sweep_tempdirs

    cleanup = sweep_tempdirs()
    profile = getattr(args, "profile", "core")
    try:
        managed_tools = managed_tools_status()
        diagnostics = build_doctor_report(profile)
        from openmd.routing.loader import effective_routes

        effective_routes()
        if profile in {"all", "tools"}:
            from openmd.routing.providers import doctor_tools

            diagnostics["tools"] = doctor_tools(probe_commands=getattr(args, "probe_commands", False))
            diagnostics["ready"] = bool(diagnostics.get("ready") and diagnostics["tools"]["ok"])
    except (OSError, ValueError) as exc:
        diagnostics = {"profile": profile, "ready": False, "error_code": "local_configuration_invalid", "error_summary": sanitize_error_summary(exc), "next_action": "Repair the explicitly selected local position configuration or route overlay; no source extraction was started."}
        managed_tools = {"opencli": "unavailable", "error_code": "local_configuration_invalid"}
    ready = diagnostics.get("ready", diagnostics.get("manifest_valid", True))
    payload = {
        "ok": ready,
        "command": "doctor",
        "temp_cleanup": cleanup,
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
        "next_action": diagnostics.get("next_action") or (
            "Use platform or sidecar UI to repair login/session/cache state, "
            "then rerun Openmd. Core records status only and does not store private session material."
        ),
        "profile": profile,
        "profile_diagnostics": diagnostics,
        "managed_tools": managed_tools,
    }
    if not getattr(args, "json", False) and sys.stdout.isatty():
        from openmd.doctor_view import render

        print(render(payload))
        # Optional capabilities never make the human view an error.
        return 0 if diagnostics.get("manifest_valid", True) and "error_code" not in diagnostics else 1
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0 if ready else 1


def cmd_routes(args: argparse.Namespace) -> int:
    from .cli import _backend_runtime_options, argparse, json

    from openmd.routing.loader import route_view

    action = args.action
    if action is None:
        view = route_view(source=args.source)
        if args.format == "json":
            print(json.dumps(view, ensure_ascii=True, sort_keys=True))
        elif args.format == "markdown":
            from openmd.routing.view import render_routes_markdown

            print(render_routes_markdown(source=args.source, effective=True), end="")
        else:
            rows = view["routes"]
            for row in rows:
                values = (row["source"], row["capability"], " → ".join(row["chain"]))
                print("\t".join(values))
        return 0
    if not args.source:
        raise ValueError("routes_source_required")
    from openmd.routing.lifecycle import demote, restore, run_benchmark

    if action == "bench":
        if not args.capability:
            raise ValueError("routes_capability_required")
        payload = run_benchmark(source=args.source, capability=args.capability,
                                candidates=args.candidates.split(",") if args.candidates else None,
                                options=_backend_runtime_options(args), ledger_path=args.ledger, sample_id=args.sample,
                                samples_path=getattr(args, "samples_file", None), artifact_dir=getattr(args, "artifacts", None))
    else:
        if not args.capability_id:
            raise ValueError("routes_capability_id_required")
        if action == "demote":
            if not args.ledger:
                raise ValueError("routes_ledger_required")
            payload = demote(args.capability_id, source=args.source, evidence_ids=args.evidence,
                             ledger_path=args.ledger, overlay_path=args.overlay, note=args.note)
        else:
            payload = restore(args.capability_id, source=args.source, overlay_path=args.overlay, note=args.note)
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    from .cli import argparse, filter_capabilities, json

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
    from .cli import argparse, build_plan, json

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
    from .cli import CliResult, _print_result, argparse

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


def cmd_report_feedback(args: argparse.Namespace) -> int:
    from .cli import CliResult, _print_result, argparse
    from pathlib import Path
    from openmd.diagnostics.feedback import build_feedback_package

    result = build_feedback_package(args.input, url=args.url, output=args.out)
    print("请检查后再附加到 GitHub issue。包内文件：" + ", ".join(result["files"]))
    _print_result(CliResult(ok=True, command="report", outputs=[str(Path(args.out))], message="feedback package written"))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .cli import CliResult, _emit_result, _process_input, _read_single_input, _start_compact_receipt, argparse, sanitize_error_summary, sys

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
    from .cli import CliResult, _append_guardrail_stop_entry, _append_large_batch_guardrail_if_needed, _compact_url_identity, _emit_result, _guardrail_failure_streak_reached, _process_input, _read_urls_file, _register_compact_inputs, _start_compact_receipt, _sync_compact_rows, argparse, sanitize_error_summary, sanitize_url, sys

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
        aggregate.paused_count += result.paused_count
        if result.next_action and aggregate.next_action is None:
            aggregate.next_action = result.next_action
            aggregate.login = result.login
            aggregate.error_code = result.error_code
        aggregate.outputs.extend(result.outputs)
        for signal in result.guardrail_signals:
            if signal not in aggregate.guardrail_signals:
                aggregate.guardrail_signals.append(signal)
        if not result.ok:
            aggregate.ok = False
            aggregate.message = "completed with failures"
            if not result.paused_count:
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
