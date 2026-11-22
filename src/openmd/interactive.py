"""Single-link workflow with human prompts and a flat Markdown destination."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from openmd.core.filename import safe_filename
from openmd.core.package_validator import validate_package_or_raise
from openmd.core.preferences import data_directory, load_preferences, save_preferences
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.tempdirs import owned_mkdtemp


_GATE_OPTIONS = {"network": "enable_network_backends", "browser": "enable_browser_backends", "media_download": "enable_media_download", "asr": "enable_asr"}


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(message: str) -> str:
    return input(message).strip()


def _say(args: argparse.Namespace, message: str) -> None:
    print(message, file=sys.stderr if getattr(args, "json", False) else sys.stdout)


def _prompt(args: argparse.Namespace, message: str) -> str:
    if getattr(args, "json", False):
        print(message, file=sys.stderr, end="", flush=True)
        return _ask("")
    return _ask(message)


def display_backend(name: object) -> str:
    """User-facing route name: the built-in browser reader is part of Openmd."""
    text = str(name or "")
    return "browser." + text[len("opencli."):] if text.startswith("opencli.") else text


def _preview(args: argparse.Namespace, url: str) -> None:
    if not getattr(args, "verbose", False):
        return
    from openmd.adapters.registry import create_default_registry
    from openmd.core.detector import detect_platform
    source = detect_platform(url)
    chain = create_default_registry().backend_chain(source, args.backend)
    _say(args, f"来源：{source}；候选工具：{' → '.join(display_backend(b) for b in chain)}")


def _authorize(args: argparse.Namespace, url: str, *, tty: bool) -> bool:
    if getattr(args, "_explicit_permissions", False):
        return True
    # Open-source ease of use (user decision 2026-11-15): a non-interactive run
    # that names a destination gets the same daily gates as the terminal flow.
    # Explicit capability flags stay a narrow grant; cloud is never implied.
    if getattr(args, "as_snapshot", False):
        return True
    _preview(args, url)
    explicit = [gate for gate, option in _GATE_OPTIONS.items() if getattr(args, option, False)]
    if getattr(args, "enable_cloud_backends", False):
        explicit.append("cloud")
    if explicit:
        # Explicit flags are the user's narrow grant; typing them is the confirmation.
        return True
    from openmd.adapters.registry import create_default_registry
    from openmd.core.detector import detect_platform
    from openmd.routing.loader import load_catalog
    catalog = load_catalog()
    chain = create_default_registry().backend_chain(detect_platform(url), args.backend)
    needed = {gate for backend in chain for gate in catalog.capabilities.get(backend, {}).get("gates", [])}
    # Daily interactive use (user decision 2026-11-07): public network, browser session reads,
    # media download and local ASR start without any prompt. Cloud is never implied.
    if getattr(args, "video", "note") in {"text", "file"}:
        needed |= {"network", "media_download", "asr"}  # asking for the video text is the grant
    requested = [gate for gate in ("network", "browser", "media_download", "asr") if gate in needed]
    for gate in requested:
        setattr(args, _GATE_OPTIONS[gate], True)
    return True


def _noninteractive_requirements(args: argparse.Namespace, url: str) -> str | None:
    if args.save_to or args.out:
        return None
    return f"非交互运行需要保存目录（-o 或 --save-to），例如：\nopenmd {url} -o <目录>"


class _StagingWorkspace:
    def __init__(self) -> None:
        self.path = Path(owned_mkdtemp(prefix="openmd-staging-"))
        self.keep = False

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_exc) -> None:
        if not self.keep:
            shutil.rmtree(self.path)


def _relocate_references(root: Path, old_root: Path, new_root: Path, *, published: dict[Path, Path] | None = None, drop_unpublished: bool = False) -> None:
    """Rewrite only structured local pointers, never source Markdown prose."""
    old = str(old_root)
    mapping = {str(before): str(after) for before, after in (published or {}).items()}

    def mapped(text: str) -> str | None:
        for before, after in mapping.items():
            if text == before:
                return after
            for separator in ("\\", "/"):
                if text.startswith(before + separator):
                    return after + text[len(before):]
        return None

    def transform(value: object, *, key: str = "") -> object:
        if isinstance(value, str):
            replacement = mapped(value)
            if replacement is not None:
                return replacement
            if key in {"artifact_ref", "output_path"} and not Path(value).is_absolute():
                replacement = mapped(str(old_root / value))
                if replacement is not None:
                    return replacement
                if drop_unpublished and (old_root / value).exists():
                    return None
            if value == old:
                return str(new_root)
            for prefix in (old + "\\", old + "/"):
                if value.startswith(prefix):
                    if key in {"artifact_ref", "output_path"} and drop_unpublished and not published:
                        return None
                    return str(new_root) + value[len(old):]
            return value
        if isinstance(value, list):
            return [transform(v, key=key) for v in value]
        if isinstance(value, dict):
            return {k: transform(v, key=k) for k, v in value.items()}
        return value

    for path in root.rglob("*"):
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps(transform(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif path.suffix == ".jsonl":
            values = [transform(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def _preserve_records(stage: Path, *, published: dict[Path, Path] | None = None) -> Path:
    target = data_directory() / "runs" / uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    if (stage / "runlog.jsonl").is_file():
        shutil.copyfile(stage / "runlog.jsonl", target / "runlog.jsonl")
    if (stage / "runs").is_dir():
        shutil.copytree(stage / "runs", target / "runs")
    # These directories only exist when the caller explicitly retained media.
    for name in ("raw-media", "raw"):
        if (stage / name).is_dir():
            shutil.copytree(stage / name, target / name)
    _relocate_references(target, stage, target, published=published, drop_unpublished=True)
    return target


def _preserve_failure_log(stage: Path) -> Path | None:
    if not (stage / "runlog.jsonl").is_file():
        return None
    return _preserve_records(stage) / "runlog.jsonl"


def _daily_filename(title: str) -> str:
    value = safe_filename(title, max_length=100)
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", value.split(".", 1)[0], re.I):
        value = "_" + value
    return value


def _save(stage: Path, destination: Path, *, title: str, package: bool = False, source: Path | None = None, sync_receipt: dict | None = None) -> Path:
    destination = destination.expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise ValueError("save_directory_required: 请输入保存文件夹")
    destination.mkdir(parents=True, exist_ok=True)
    if package:
        target = destination / f"{_daily_filename(title)}-{uuid4().hex[:8]}"

        def ignore_records(directory: str, names: list[str]) -> list[str]:
            if Path(directory) == stage:
                return [name for name in names if name in {".openmd-compact.lock", ".openmd-compact.guard", ".openmd-owner.json", "runlog.jsonl", "runs"}]
            return []

        shutil.copytree(stage, target, ignore=ignore_records)
        _relocate_references(target, stage, target)
        for path in target.rglob("*.openmd"):
            validate_package_or_raise(path)
        return target
    if source is None:
        matches = list(stage.rglob("*.md"))
        if len(matches) != 1:
            raise ValueError("go_single_markdown_required: 本次产生多份文件，请显式选择 --package 保存")
        source = matches[0]
    if source.suffix != ".md" or not source.resolve().is_relative_to(stage.resolve()):
        raise ValueError("go_single_markdown_required: 未找到本次提取的 Markdown")
    base = _daily_filename(title)
    staged_manifest = stage / ".openmd/manifest.json"
    if staged_manifest.is_file():
        from openmd.core.sync_manifest import SyncManifest

        entries = json.loads(staged_manifest.read_text(encoding="utf-8")).get("entries", {})
        relative = source.resolve().relative_to(stage.resolve()).as_posix()
        matches = [entry for entry in entries.values() if entry.get("path") == relative]
        if len(matches) != 1:
            raise ValueError("go_sync_source_identity_missing")
        entry = matches[0]
        synced = SyncManifest(destination).write(entry["source_id"], f"{base}.md", source.read_text(encoding="utf-8"),
                                                 content_identity=entry.get("content_identity"))
        if sync_receipt is not None:
            sync_receipt["operation"] = synced.operation
            sync_receipt.pop("renamed_from", None)
            if synced.renamed_from is not None:
                sync_receipt["renamed_from"] = synced.renamed_from
        return synced.path
    for attempt in range(100):
        target = destination / f"{base}{'-' + uuid4().hex[:6] if attempt else ''}.md"
        try:
            handle = target.open("xb")
        except FileExistsError:
            continue
        try:
            with handle:
                assets = source.parent / "assets"
                if assets.is_dir():
                    asset_target = destination / f"{target.stem}.assets-{uuid4().hex[:6]}"
                    shutil.copytree(assets, asset_target)
                    text = source.read_text(encoding="utf-8")
                    text = re.sub(r"(!\[[^\]]*\]\(<?)assets/", lambda m: m.group(1) + asset_target.name + "/", text)
                    handle.write(text.encode("utf-8"))
                else:
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, handle)
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    raise OSError("save_name_collision: 无法分配新文件名，请换一个保存目录")


def _source_summary(stage: Path, result) -> tuple[str, list[dict[str, object]]]:
    from openmd.core.renderer import source_level_label

    runlog = stage / "runlog.jsonl"
    rows = [json.loads(line) for line in runlog.read_text(encoding="utf-8").splitlines() if line.strip()] if runlog.is_file() else []
    levels = [str(row["source_level"]) for row in rows if row.get("event_type") == "item" and row.get("source_level")]
    if not levels:
        levels = list(result.source_level_counts)
    labels = list(dict.fromkeys(source_level_label(level) for level in levels))
    return " / ".join(labels), rows


def _failure_hint(rows: list[dict[str, object]]) -> str:
    from openmd.core.login import login_for_url, login_hint, is_login_code

    for row in reversed(rows):
        if is_login_code(row.get("error_code")):
            login = row.get("login") or login_for_url(str(row.get("input_url") or ""), str(row.get("platform") or ""))
            return login_hint(login)
    from openmd.cli_login import source_next_action

    # One owner for source-specific next steps: the same text reaches the
    # human line and the JSON receipt's next_action.
    for row in reversed(rows):
        action = source_next_action(str(row.get("error_code") or ""))
        if action:
            return action
    codes = " ".join(str(row.get("error_code") or "") for row in rows).lower()
    if "browser_challenge_page" in codes:
        return "被站点验证页拦截，未取得目标正文；请在浏览器中确认原链接可访问后重试。"
    if "weixin_error_page" in codes:
        return "公众号返回了错误页（链接失效、已删除或已过期），没有正文；请在微信或浏览器中打开确认。"
    if "content_navigation_shell" in codes:
        return "页面只有导航或图片，没有正文；链接可能不存在（软 404）或需要登录，请在浏览器中打开确认。"
    if "engine_unavailable" in codes:
        for row in reversed(rows):
            if row.get("error_code") == "engine_unavailable" and row.get("error_summary"):
                return str(row["error_summary"])
        return "所需文档引擎未安装；请按 next_action 安装对应引擎后重试。"
    attempts = [a for row in rows if isinstance(row.get("route_trace"), dict) for a in row["route_trace"].get("fallback_attempts") or []]
    opencli_missing = any(str(a.get("backend", "")).startswith("opencli.") and a.get("error_code") in {"backend_exit_127", "backend_not_found"} for a in attempts)
    content_verdict = any(word in codes for word in ("weixin_error_page", "not_found", "404"))
    if opencli_missing and not content_verdict:
        return "这个链接要用浏览器读取组件，但自动准备没有成功；检查网络（或设置 OPENMD_NPM_REGISTRY 使用 npm 镜像）后重试，运行 openmd doctor 查看详情。"
    opencli_codes = {str(a.get("error_code")) for a in attempts if str(a.get("backend", "")).startswith("opencli.")}
    if "backend_exit_69" in opencli_codes and not content_verdict:
        return "这个链接要借用浏览器读取：请先运行 openmd browser-setup 装好 Openmd 浏览器插件，保持 Chrome / Edge 打开后重试。"
    if "backend_exit_77" in opencli_codes and not content_verdict:
        return "这个链接需要登录：请先在 Chrome / Edge 里登录该网站，再重试。"
    if any(word in codes for word in ("model_missing", "asr_model", "worker")):
        return "本机转录环境或模型尚未就绪；运行 openmd doctor --profile media 查看缺项。"
    if any(word in codes for word in ("auth", "login", "cookie", "browser", "permission")):
        return "当前读取方式需要浏览器或现有登录状态；确认浏览器可访问原链接后重试。"
    if "backend_not_implemented" in codes:
        return "当前读取路线尚未实现；可加 --verbose 查看具体路线。"
    if any(word in codes for word in ("dependency", "not_installed", "not_registered")):
        return "所需工具尚未就绪；运行 openmd doctor --profile tools 查看缺项。"
    if any(word in codes for word in ("timeout", "network", "connection")):
        return "连接超时或网络不可达；确认原链接可以打开后重试。"
    return "没有取得可保存的正文；可加 --verbose 查看具体工具错误，或确认原链接能正常打开。"


def issue_context(url: str, rows: list[dict[str, object]]) -> str:
    """One pasteable line for the "链接提取失败" issue form; no paths, no secrets."""
    import platform as host

    from openmd import __version__
    from openmd.core.detector import detect_platform

    tried = []
    for row in rows:
        attempts = (row.get("route_trace") or {}).get("fallback_attempts") if isinstance(row.get("route_trace"), dict) else None
        if attempts:
            tried += [f"{display_backend(a.get('backend'))}={a.get('error_code')}" for a in attempts]
        elif row.get("event_type") == "item" or row.get("error_code"):
            tried.append(f"{display_backend(row.get('backend'))}={row.get('error_code') or row.get('status')}")
    # Sanitize each attempt: one joined string would hit the summary length cap and lose the tail.
    route = ", ".join(dict.fromkeys(str(sanitize_error_summary(part)) for part in tried)) or "无工具记录"
    # Local files all route through the generic document platform (internally "pdf");
    # name the file type instead so an EPUB failure is not reported as a PDF one.
    source = detect_platform(url) if "://" in url else f"本地文件 {Path(url).suffix.lower() or '(无扩展名)'}"
    return f"openmd {__version__} · {host.system()} {host.release()} · Python {host.python_version()} · 平台 {source} · {route}"


def _emit_saved(args: argparse.Namespace, *, target: Path, outputs: list[str], records: Path | None) -> None:
    if getattr(args, "json", False):
        result = getattr(args, "_go_result", None)
        print(json.dumps({"ok": True, "command": "go", "saved_to": str(target), "outputs": outputs, "records": str(records) if records else None, **_go_receipt_fields(result, None, ok=True)}, ensure_ascii=True))
    else:
        print(f"已保存：{target}")
        if getattr(args, "verbose", False) and records:
            print(f"运行记录：{records}")


def _go_receipt_fields(result, rows: list[dict[str, object]] | None, *, ok: bool) -> dict[str, object]:
    row = next((r for r in reversed(rows or []) if r.get("event_type") == "item"), {})
    qr_path = getattr(result, "qr_path", None)
    qr_expires_at = getattr(result, "qr_expires_at", None)
    if qr_path and type(qr_expires_at) is not int:
        from openmd.platforms.xiaoyuzhou_auth import pending_qr_expiry
        qr_expires_at = pending_qr_expiry(qr_path=qr_path)
    error_code = row.get("error_code") or getattr(result, "error_code", None)
    next_action = getattr(result, "next_action", None)
    if not next_action and not ok and error_code:
        from openmd.cli_login import source_next_action
        # Same owner as the human failure line (I082): row-level codes such as
        # douyin_detail_read_failed must not leave the JSON next_action empty.
        next_action = source_next_action(str(error_code))
    return {
        "status": row.get("status") or getattr(result, "status", None) or ("verified" if ok else "failed"),
        "source_level": row.get("source_level") or getattr(result, "source_level", None),
        "completeness": row.get("completeness") or getattr(result, "completeness", {}) or {},
        "sync": row.get("sync") or getattr(result, "sync", {}) or {},
        "login": row.get("login") or getattr(result, "login", None),
        "next_action": next_action,
        "qr_path": qr_path,
        "qr_expires_at": qr_expires_at,
        "qr_http_status": getattr(result, "qr_http_status", None),
        "error_code": error_code,
    }


def _attach_video(args: argparse.Namespace, url: str, markdown: Path, *, stage: Path | None = None) -> None:
    from openmd.post_video import (
        append_media_retention_note,
        append_retained_media_section,
        append_video_section,
        download_native_media_only,
        native_media_source,
        native_transcript_source,
        should_process,
    )

    mode = getattr(args, "video", "note") or "note"
    native_source = native_transcript_source(url)
    if not getattr(args, "enable_network_backends", False):
        return
    if mode == "file" and native_source == "xiaoe":
        append_media_retention_note(markdown, "本站点转写路线未提供可安全保留的公开解码媒体；受加密或访问控制的流仅用于转写")
        return
    media_source = native_media_source(url) if mode == "file" else None
    media_kind = "audio" if media_source == "xiaoyuzhou" else "video" if media_source == "bilibili" else "video/音频"
    if mode == "file" and native_source is not None:
        if stage is not None and append_retained_media_section(markdown, stage / "raw-media", media_kind=media_kind):
            return
        if media_source is not None:
            if not getattr(args, "enable_media_download", False) or stage is None:
                append_media_retention_note(markdown, "未开启媒体下载；在命令行加 --enable-media-download，或从扩展右键菜单选择保存文件")
                return
            media_dir = stage / "raw-media"
            media_path = download_native_media_only(url, media_dir, media_kind=media_kind)
            if media_path is not None and append_retained_media_section(markdown, media_dir, media_kind=media_kind):
                return
        # No native route artifact was retained (for example YouTube subtitles
        # do not download media). If a native media tool failed, fall back to
        # post_video's bounded downloader without transcribing a second time.
        append_video_section(markdown, url, mode, transcribe_media=False, media_kind=media_kind)
        return
    if not should_process(url, mode):
        return
    if mode in {"text", "file"} and not (getattr(args, "enable_media_download", False) and getattr(args, "enable_asr", False)):
        mode = "note"  # video text needs explicit media download plus local ASR
    if mode != "note":
        _say(args, "正在处理帖子里的视频/音频（下载并在本机转文字）…")
    try:
        append_video_section(markdown, url, mode)
    except OSError as exc:
        print(f"视频部分未完成：{sanitize_error_summary(exc)}", file=sys.stderr)


def cmd_go(args: argparse.Namespace) -> int:
    """Save one input; only a human terminal save remembers its destination.

    Both stdin and stdout must be TTYs, and neither --json (machine receipt)
    nor --yes (unattended caller) may be set. A PTY attached to automation does
    not make these explicit machine calls interactive. Batch has no preference
    write; the extension keeps its human-selected destination in browser state.
    """
    from openmd.cli import _emit_result, _process_input, _start_compact_receipt
    tty = is_interactive()
    if any(getattr(args, option, False) for option in ("all", "overwrite", "update", "new_version")) or args.package_root:
        print("openmd go: 批量与更新操作请使用 ingest/batch/collect 及明确输出目录", file=sys.stderr)
        return 2
    try:
        url = args.url or (_prompt(args, "输入链接或本地文件：") if tty else "")
        if url == "-":
            if tty:
                print("openmd go: 请直接粘贴链接", file=sys.stderr)
                return 2
            url = sys.stdin.readline().strip()
        if not url:
            print("openmd go: 需要一个链接或本地文件", file=sys.stderr)
            return 2
        from openmd.importers.chatgpt import cmd_import, is_chatgpt_export
        from openmd.importers.claude import cmd_import as import_claude, is_claude_export

        if getattr(args, "chatgpt_export", False) and getattr(args, "claude_export", False):
            raise ValueError("official_export_format_flags_conflict")
        if getattr(args, "claude_export", False):
            return import_claude(args, url, tty=tty)
        if getattr(args, "chatgpt_export", False):
            return cmd_import(args, url, tty=tty)
        if is_claude_export(url):
            return import_claude(args, url, tty=tty)
        if is_chatgpt_export(url):
            return cmd_import(args, url, tty=tty)
        if getattr(args, "as_snapshot", False):
            source_url = str(getattr(args, "snapshot_url", "") or "").strip()
            if not re.match(r"^https?://[^\s]+$", source_url):
                print("openmd go: --as-snapshot 需要 --url <原始 http(s) URL>", file=sys.stderr)
                return 2
        from openmd.core.input_validation import INPUT_HINT, network_preflight, valid_daily_input

        if not valid_daily_input(url):
            print(INPUT_HINT, file=sys.stderr)
            return 2
        if not tty:
            requirements = _noninteractive_requirements(args, url)
            if requirements:
                print(requirements, file=sys.stderr)
                return 2
        if not _authorize(args, url, tty=tty):
            _say(args, "已取消。")
            return 0
        if not getattr(args, "as_snapshot", False) and args.backend != "mock" and getattr(args, "enable_network_backends", False) and url.startswith(("http://", "https://")):
            failure = network_preflight(url)
            if failure:
                print(failure, file=sys.stderr)
                return 1
        # Incremental export needs the persistent output manifest before extraction.
        from openmd.adapters.registry import create_default_registry

        adapter = None if getattr(args, "as_snapshot", False) else create_default_registry().adapter_for(url)
        if getattr(adapter, "sync_cursor_enabled", False):
            destination = args.save_to or args.out
            if destination is None and tty:
                destination = _prompt(args, "同步保存目录：")
            if not destination:
                raise ValueError("sync_output_directory_required")
            run_args = argparse.Namespace(**vars(args))
            run_args.out = str(Path(destination).expanduser().resolve())
            run_args.url = url
            if error := _start_compact_receipt(run_args, "go"):
                raise ValueError(error)
            try:
                return _emit_result(run_args, _process_input(url, run_args, command="go"))
            finally:
                if lock := getattr(run_args, "_compact_output_lock", None):
                    lock.release()
        workspace = _StagingWorkspace()
        with workspace as temporary:
            stage = Path(temporary)
            requested_destination = args.save_to or args.out
            run_args = argparse.Namespace(**vars(args))
            run_args.out = str(stage)
            run_args.url = url
            run_args.package_root = None
            run_args.runlog = None
            run_args.command = "go"
            if getattr(args, "video", "note") == "file":
                run_args.keep_media = True
            start_error = _start_compact_receipt(run_args, "go")
            if start_error:
                raise ValueError(start_error)
            try:
                result = _process_input(url, run_args, command="go")
                run_args._go_result = result
                args._go_result = result
                if run_args.receipt:
                    with contextlib.redirect_stdout(io.StringIO()):
                        receipt_code = _emit_result(run_args, result)
                    if receipt_code and result.ok:
                        raise ValueError("go_receipt_failed")
                source_label, rows = _source_summary(stage, result)
                if not result.ok or not result.outputs:
                    print(f"处理失败：{_failure_hint(rows)}", file=sys.stderr)
                    print(f"提 issue 请附上：{issue_context(url, rows)}", file=sys.stderr)
                    if getattr(args, "verbose", False):
                        for row in rows[-3:]:
                            print(f"{display_backend(row.get('backend'))}: {row.get('error_code')}；{sanitize_error_summary(row.get('error_summary'))}", file=sys.stderr)
                    retained = _preserve_failure_log(stage)
                    if retained:
                        print(f"错误记录：{retained}", file=sys.stderr)
                    if getattr(args, "json", False):
                        print(json.dumps({"ok": False, "command": "go", "error": sanitize_error_summary(result.message) or "extraction_failed", "records": str(retained) if retained else None,
                                          **_go_receipt_fields(result, rows, ok=False)}, ensure_ascii=True))
                    return 1
                for output in result.outputs:
                    if Path(output).is_dir():
                        validate_package_or_raise(Path(output))
                first = Path(result.outputs[0])
                if not args.package and len(result.outputs) != 1:
                    workspace.keep = True
                    raise ValueError(f"go_single_markdown_required: 本次产生多份文件；请使用 --package。已提取产物保留在：{stage}")
                title = first.stem
                if first.is_dir():
                    meta = json.loads((first / "metadata.json").read_text(encoding="utf-8"))
                    title = str(meta.get("title") or title)
                if not args.package and first.suffix == ".md":
                    _attach_video(args, url, first, stage=stage)
                elif args.package and first.is_dir() and (first / "index.md").is_file():
                    _attach_video(args, url, first / "index.md", stage=stage)
                label = "已取得部分正文" if any(row.get("status") == "partial" for row in rows) else "已取得正文"
                _say(args, f"{label}：{title}" + (f"；来源：{source_label}" if source_label else ""))
                if getattr(args, "verbose", False):
                    tools = ", ".join(dict.fromkeys(display_backend(row["backend"]) for row in rows if row.get("backend")))
                    _say(args, f"工具：{tools}")
                default_destination = str(load_preferences().get("last_save_directory") or Path.cwd() / "out")
                destination_text = requested_destination
                while True:
                    if destination_text is None:
                        destination_text = _prompt(args, f"保存在哪里？[回车用 {default_destination}；n 取消] ") or default_destination
                    if destination_text.lower() in {"n", "no", "取消"}:
                        _preserve_records(stage)
                        _say(args, "已取消保存。")
                        return 0
                    try:
                        destination = Path(destination_text.strip().strip('"')).expanduser().resolve()
                        published_sync = {}
                        target = _save(stage, destination, title=title, package=args.package, source=None if args.package else first, sync_receipt=published_sync)
                        if published_sync:
                            from dataclasses import replace

                            args._go_result = replace(result, sync=published_sync)
                        break
                    except (OSError, ValueError) as exc:
                        if not tty or requested_destination:
                            workspace.keep = True
                            print(f"保存未完成，已提取产物保留在：{stage}", file=sys.stderr)
                            raise
                        print(f"保存失败：{sanitize_error_summary(exc)}", file=sys.stderr)
                        destination_text = None
                published = {Path(p): target / Path(p).relative_to(stage) if args.package else target for p in result.outputs}
                outputs = [str(published[Path(p)]) for p in result.outputs]
                records = None
                try:
                    records = _preserve_records(stage, published=published)
                except (OSError, ValueError) as exc:
                    workspace.keep = True
                    print(f"正文已保存；运行记录暂留：{stage}（{sanitize_error_summary(exc)}）", file=sys.stderr)
                if (tty and not getattr(args, "json", False) and not getattr(args, "yes", False)
                        and not getattr(args, "_explicit_permissions", False)):
                    try:
                        save_preferences(last_save_directory=destination)
                    except OSError as exc:
                        print(f"正文已保存；未能记住保存目录：{sanitize_error_summary(exc)}", file=sys.stderr)
                _emit_saved(args, target=target, outputs=outputs, records=records)
                return 0
            finally:
                lock = getattr(run_args, "_compact_output_lock", None)
                if lock is not None:
                    lock.release()
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"openmd go: {sanitize_error_summary(exc)}", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "command": "go", "error": sanitize_error_summary(exc)}, ensure_ascii=True))
        return 1
