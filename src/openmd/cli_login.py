"""Login recovery for single and batch CLI extraction."""

from __future__ import annotations

import sys

from openmd.core.login import login_action, login_for_url, login_hint, normalize_login_result, prompt_login_retry


def paused_login(args, url):
    return getattr(args, "_login_paused", {}).get(login_for_url(url)["url"])


def recovery_fields(args):
    qr_path = getattr(args, "_xiaoyuzhou_qr_path", None)
    if qr_path:
        qr_expires_at = getattr(args, "_xiaoyuzhou_qr_expires_at", None)
        return {"error_code": "login_required", "login": {"site": "小宇宙", "url": ""},
                "qr_path": qr_path, "qr_expires_at": qr_expires_at,
                "qr_http_status": getattr(args, "_xiaoyuzhou_qr_http_status", None),
                "next_action": f"请用小宇宙 App 扫码：{qr_path}"}
    auth_action = getattr(args, "_xiaoyuzhou_auth_next_action", None)
    if auth_action:
        return {"error_code": "login_required", "login": {"site": "小宇宙", "url": ""},
                "qr_http_status": getattr(args, "_xiaoyuzhou_qr_http_status", None),
                "next_action": auth_action}
    values = list(getattr(args, "_login_paused", {}).values())
    if not values:
        return {}
    login = values[0]
    return {"error_code": "login_required", "login": login, "next_action": login_action(login)}


def source_next_action(error_code: str | None) -> str | None:
    """Return a bounded user route for source-level blocks."""
    if error_code == "web_error_page":
        return "网页返回了限流或错误页，没有保存为正文；请稍后重试同一链接。"
    if error_code in {"web_fetch_timeout", "web_fetch_connection_error", "web_fetch_http_error", "web_fetch_failed"}:
        return "网页取页已尝试代理与直连重试；请检查网络或稍后重试同一链接。"
    if error_code == "arxiv_full_text_unavailable":
        return "只取得 arXiv 摘要页，未保存为全文；可稍后重试，或在 arXiv 查看是否提供 HTML 全文版。"
    if error_code in {"xiaohongshu_request_blocked", "xiaohongshu_loaded_page_mismatch"}:
        return "在浏览器打开这篇小红书笔记后，点击 Openmd 扩展保存；Openmd 将读取当前已加载页面。"
    if error_code == "douyin_snapshot_no_transcript":
        return "抖音视频需走媒体转写：openmd go <链接>（扩展点击会走链接路线）"
    if error_code in {"xiaohongshu_signed_url_required", "opencli_xiaohongshu_timeout"}:
        return ("在浏览器里打开这条小红书笔记后点 Openmd 扩展保存，或从分享按钮复制完整链接再试；"
                "Agent 可把已加载的页面交给 openmd go <页面.html> --as-snapshot --url <链接>。")
    if error_code in {"douyin_asr_unavailable", "asr_model_missing", "asr_runtime_unavailable", "asr_backend_missing"}:
        return "本机还没有语音模型：运行 openmd models install asr:baseline（约 1.6 GB，会先确认）后重试。"
    if error_code in {"media_download_backend_missing", "bilibili_media_asr_not_enabled", "asr_backend_missing", "bilibili_media_asr_dependency_missing"}:
        return '安装媒体依赖：pip install "openmd-cli[media]"；再准备本地语音模型 openmd models install asr:baseline 后重试。'
    if error_code in {"ocr_backend_missing", "ollama_model_missing", "ollama_not_reachable"}:
        return "本机还没有可用的 OCR：安装并启动 Ollama 后运行 openmd models install ocr:chain（会先确认），再重试。"
    if error_code == "headless_browser_unavailable":
        return "当前是无头环境，该来源需要浏览器或登录态；提供公开 HTTP/CLI 路线，或在有浏览器的环境重试。"
    if error_code == "document_invalid_container":
        return "文件结构不完整或已损坏（常见于没下载完）：请重新下载或用原软件另存一份后再试。"
    if error_code == "douyin_detail_read_failed":
        return "抖音需要借用浏览器读取：确认 Chrome / Edge 已登录抖音、Openmd 扩展已加载（openmd browser-setup）后重试。"
    if error_code == "kaze_launcher_missing":
        return "未找到 Kaze BrowserMCP 启动器；安装或修复 Kaze 后重试。"
    if error_code == "kaze_not_running":
        return "请启动 Kaze，并在 Kaze 设置中允许 External Agents 后重试。"
    if error_code == "kaze_unavailable":
        return "Kaze BrowserMCP 当前不可用；确认 Kaze 正在运行且 External Agents 已启用后重试。"
    if error_code == "kaze_external_agents_disabled":
        return "请在 Kaze 设置中允许 External Agents 后重试；Openmd 不会更改该设置。"
    if error_code in {"kaze_unsupported_protocol", "kaze_tool_schema_changed"}:
        return "Kaze 的 BrowserMCP 接口与 Openmd 不兼容；更新 Kaze 后重试，或使用其他 Openmd 路线。"
    if error_code == "kaze_timeout":
        return "Kaze BrowserMCP 响应超时；确认 Kaze 正在运行且 External Agents 已启用后重试。"
    if error_code == "kaze_script_error":
        return "Kaze 拒绝了页面采集脚本参数；这是 Openmd 与 BrowserMCP 的调用兼容问题，请更新 Openmd 后重试。"
    if error_code == "kaze_page_not_ready":
        return "页面仍在加载；等 Kaze 页面完成加载后重试。"
    if error_code == "kaze_tab_open_failed":
        return "Kaze 未返回可用页面标签；确认 Kaze 可打开目标网址后重试。"
    if error_code == "kaze_snapshot_transfer_failed":
        return "Kaze 未能把页面快照写入 Openmd 临时目录；检查临时存储可用性后重试。"
    if error_code == "kaze_snapshot_too_large":
        return "Kaze 页面快照超过 Openmd 限制；请使用页面导出或缩小读取范围后重试。"
    if error_code == "kaze_cleanup_failed":
        return "Kaze 标签清理未能确认；先检查 Kaze 标签状态，再重试。"
    if error_code == "kaze_page_url_mismatch":
        return "Kaze 当前页面与请求链接不一致；请在 Kaze 打开目标链接后重试。"
    if error_code == "kaze_login_required":
        return "Kaze 当前页是登录墙；由用户在 Kaze 登录并打开目标页面后，再运行 Openmd。"
    if error_code == "kaze_snapshot_failed":
        return "Kaze 未能生成页面快照；检查当前页面和 BrowserMCP 状态后重试。"
    return None


def extract_with_login(executor, item, chain, args):
    from openmd.interactive import is_interactive

    raw = executor.extract(item, chain, allow_fallback=args.fallback)
    if item.platform == "xiaoyuzhou" and raw.error_code == "login_required":
        path = raw.metadata.get("qr_path")
        action = raw.metadata.get("next_action")
        if path:
            args._xiaoyuzhou_qr_path = path
            expires_at = raw.metadata.get("qr_expires_at")
            if type(expires_at) is not int:
                from openmd.platforms.xiaoyuzhou_auth import pending_qr_expiry
                expires_at = pending_qr_expiry(qr_path=path)
            args._xiaoyuzhou_qr_expires_at = expires_at
            return raw
        if isinstance(action, str):
            args._xiaoyuzhou_auth_next_action = action
            status = raw.metadata.get("qr_http_status")
            if type(status) is int:
                args._xiaoyuzhou_qr_http_status = status
            return raw
    result = normalize_login_result(raw, item)
    if result.error_code != "login_required":
        return result
    login = result.metadata["login"]
    seen = getattr(args, "_login_notified", set())
    args._login_notified = seen
    key = login["url"]
    if key not in seen:
        seen.add(key)
        print(login_hint(login), file=sys.stderr)
        if is_interactive() and not getattr(args, "json", False) and not getattr(args, "receipt", None):
            if prompt_login_retry(login):
                result = normalize_login_result(executor.extract(item, chain, allow_fallback=args.fallback), item)
                if result.error_code != "login_required":
                    return result
    paused = getattr(args, "_login_paused", {})
    args._login_paused = paused
    paused[key] = login
    return result


def paused_result(args, command, login):
    from openmd.cli import CliResult

    return CliResult(ok=False, command=command, item_count=1, paused_count=1,
                     message="paused pending login", error_code="login_required",
                     login=login, next_action=login_action(login))
