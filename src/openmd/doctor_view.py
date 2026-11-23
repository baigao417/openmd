"""Human-readable `openmd doctor`: what works now, what to install for more.

The JSON payload stays the machine contract (`--json`, or any non-TTY stdout);
this view only answers a new user's question in plain words.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Any


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _browser_registered() -> bool | None:
    if os.name != "nt":
        return None
    from openmd.browser_setup import host_files

    return host_files()[0].is_file()


def _kaze_running() -> bool:
    """Detect the supported Kaze desktop process without reading its settings."""
    # A running Kaze serves named pipes "kaze-<id>-mcp"; listing them starts no
    # process, so doctor stays within the hidden-window/managed-wait contract.
    if os.name != "nt":
        return False
    try:
        pipes = os.listdir(r"\\.\pipe" + "\\")
    except OSError:
        return False
    return any(name.lower().startswith("kaze-") and name.lower().endswith("-mcp") for name in pipes)


def _opencli_row() -> tuple[bool | None, str, str]:
    from openmd.runtime.managed_tools import status

    state = status()
    label = "B 站字幕、公众号、X 等需要浏览器读取的平台"
    if state["opencli"] in {"user", "managed"}:
        return True, label + "（还需 Openmd 浏览器插件：openmd browser-setup）", ""
    how = "首次处理这类链接时自动准备（约 20MB）" if state["bundled_node"] and state["auto_install"] else "pip install --upgrade openmd-cli"
    return False, label, how + "；并运行 openmd browser-setup 装好浏览器插件"


def capability_rows(payload: dict[str, Any]) -> list[tuple[bool | None, str, str]]:
    """Return (ready, ability, how-to-enable) rows; ready=None means not applicable."""
    diagnostics = payload.get("profile_diagnostics") or {}
    asr_probe = (diagnostics.get("asr_runtime") or {}).get("probe", {})
    asr_ready = bool(asr_probe.get("ready") or asr_probe.get("fallback_available"))
    documents = ((diagnostics.get("optional_profiles") or {}).get("documents") or {})
    formats = diagnostics.get('document_formats') or {}
    kaze = diagnostics.get("kaze_snapshot") or {}
    native_ready = any(formats.get(suffix, {}).get('available') for suffix in ('pdf', 'docx', 'pptx', 'xlsx'))
    rows: list[tuple[bool | None, str, str]] = [
        (_has("trafilatura") and _has("youtube_transcript_api"), "网页文章、YouTube 有字幕的视频", "pip install --upgrade openmd-cli"),
        (bool((diagnostics.get("yt_dlp") or {}).get("version")), "保存公开视频 / 音频文件", 'pip install "openmd-cli[media]"'),
        _opencli_row(),
        (bool((diagnostics.get("profile_diagnostics") or {}).get("ffmpeg_source") != "missing"), "音视频处理（本机转录前置）", '安装 openmd-cli[media] 或系统 FFmpeg'),
        (asr_ready, "无字幕视频 / 播客本机转录", 'pip install "openmd-cli[asr]"，再按 docs/tool-integration.md 准备模型'),
        (native_ready or bool(documents.get("ocr_dependency_available") or documents.get("markitdown_available")), "PDF、Office 原生文字", 'pip install "openmd-cli[document]"'),
        (bool(documents.get("ocr_dependency_available")), "图片 / 扫描件 OCR", 'pip install "openmd-cli[ocr]"；高级 OCR 见 docs/tool-integration.md'),
        ((bool(diagnostics['browser_host']['ready']) if diagnostics['browser_host']['supported'] else None)
         if 'browser_host' in diagnostics else _browser_registered(), "浏览器插件一键保存",
         (diagnostics.get('browser_host') or {}).get('next_action') or "openmd browser-setup"),
        (bool(kaze.get("available")), "Kaze 已加载页面读取",
         kaze.get("next_action") or "设置 OPENMD_KAZE=0 可关闭 Kaze 路线"),
    ]
    for tool in ((diagnostics.get("tools") or {}).get("tools") or []):
        rows.append((bool(tool.get("resolved")), f"可选工具 {tool.get('tool')}", "见 docs/tool-integration.md"))
    for suffix, state in (diagnostics.get('document_formats') or {}).items():
        available = [row['id'] for row in state['engines'] if row['available']]
        missing = [row['id'] for row in state['engines'] if not row['available']]
        label = suffix.upper() + '：' + (' → '.join(available) or '缺少本地引擎')
        if missing:
            label += '；未安装：' + '、'.join(missing)
            label += '（' + '；'.join(dict.fromkeys(row['install_command'] for row in state['engines'] if not row['available'])) + '）'
        if suffix in {'doc', 'ppt'}:
            label += '；无 LibreOffice 时仅 partial 文字回退'
        rows.append((state['available'], label, state['install_command'] + '；' + state['scope']))
    return rows


def render(payload: dict[str, Any]) -> str:
    rows = [row for row in capability_rows(payload) if row[0] is not None]
    lines = ["Openmd 体检", ""]
    if _kaze_running():
        lines += [
            "  Kaze：请把 Openmd 固定到 Kaze 工具栏，才能看到保存徽标；Kaze 暂不支持 chrome.debugger，",
            "      B 站字幕等需要调试接口的读取会自动尝试其他路线，无法读取时会提示。",
        ]
    xiaoyuzhou = (payload.get("profile_diagnostics") or {}).get("xiaoyuzhou_login")
    if xiaoyuzhou:
        lines.append(f"  小宇宙：{xiaoyuzhou}")
    kaze = (payload.get("profile_diagnostics") or {}).get("kaze_snapshot") or {}
    if kaze:
        version = lambda key: kaze.get(key) or "MCP 未提供"
        lines.append(
            "  Kaze 组件版本："
            f"Kaze {version('kaze_version')}；Bridge {version('bridge_version')}；"
            f"BrowserMCP server {version('server_version')}；协议 {version('protocol_version')}；"
            f"工具 schema {kaze.get('schema_fingerprint') or '不可用'}"
        )
    for ready, ability, how in rows:
        lines.append(f"  {'✓' if ready else '·'} {ability}" + ("" if ready else f"\n      → {how}"))
    missing = sum(1 for ready, *_ in rows if not ready)
    lines += ["", "日常使用：openmd \"链接\"     脚本 / Agent：openmd \"链接\" -o <目录>"]
    if missing:
        lines.append(f"{missing} 项可选能力未就绪；不装也能用，遇到对应内容时会提示。机器可读结果：openmd doctor --json")
    preparation = (payload.get("profile_diagnostics") or {}).get("model_preparation") or {}
    recommendation = (payload.get('profile_diagnostics') or {}).get('model_recommendation') or {}
    if recommendation:
        lines += ['', f"本机建议：OCR {recommendation['ocr']}；ASR {recommendation['asr']}（{recommendation['validation']}）",
                  '安装 ASR 模型：openmd models install ' + recommendation['asr'] + '；首次安装会询问确认。']
        discovered = (payload.get('profile_diagnostics') or {}).get('discovered_models') or []
        lines.append('已发现 ASR 模型：' + ('；'.join(f"{item.get('name')}（{'已评测' if item.get('evaluated') else '未经 Openmd 评测'}）" for item in discovered) if discovered else '无'))
    managed = payload.get("managed_tools") or {}
    if managed.get("executable"):
        lines += ["", f"OpenCLI: {managed['executable']}",
                  f"版本: {managed.get('actual_version') or '未安装'}；配置目录: {managed.get('config_dir')}",
                  f"插件: {', '.join(managed.get('plugins') or []) or '无'}"]
        if managed.get("ignored_path_opencli"):
            lines.append(f"PATH 上的其他 opencli 已忽略: {managed['ignored_path_opencli']}")
    yt_dlp = (payload.get("profile_diagnostics") or {}).get("yt_dlp") or {}
    if yt_dlp:
        origin = "Openmd 管理环境" if yt_dlp.get("source") == "managed" else "PATH" if yt_dlp.get("source") == "path" else "未安装"
        age = f"；发布 {yt_dlp['age_days']} 天" if yt_dlp.get("age_days") is not None else ""
        lines += ["", f"yt-dlp: {origin}；版本 {yt_dlp.get('version') or '未知'}{age}"]
        if yt_dlp.get("warning"):
            lines.append(f"  → {yt_dlp.get('next_action') or '运行 openmd doctor 查看版本。'}")
    if preparation.get("prepare_recommended"):
        lines.append("本机模型大分片会增加加载内存；运行 openmd asr prepare-models 准备独立小分片副本（原模型保留）。")
    patches = (payload.get('profile_diagnostics') or {}).get('opencli_patches') or {}
    if patches:
        lines.append(f"OpenCLI 补丁：{patches.get('total', 0)} 条；全部匹配：{'是' if patches.get('ok') else '否'}")
        lines.extend(f"  {p['id']}: {p['error_code']}" for p in patches.get('patches', []) if not p['ok'])
    verification = (payload.get('profile_diagnostics') or {}).get('route_verification') or {}
    for route in verification.get('routes', []):
        if route['state'] == 'stale':
            lines.append(f"路线 {route['source']}/{route['capability']} 已 {route['age_days']} 天未验证；请用授权真实样本复验。")
    if verification.get('unknown_count'):
        lines.append(f"{verification['unknown_count']} 条路线尚无已登记的真实验证日期。")
    return "\n".join(lines)
