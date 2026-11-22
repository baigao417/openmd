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
    if error_code in {"xiaohongshu_request_blocked", "xiaohongshu_loaded_page_mismatch"}:
        return "在浏览器打开这篇小红书笔记后，点击 Openmd 扩展保存；Openmd 将读取当前已加载页面。"
    if error_code == "douyin_snapshot_no_transcript":
        return "抖音视频需走媒体转写：openmd go <链接>（扩展点击会走链接路线）"
    if error_code in {"xiaohongshu_signed_url_required", "opencli_xiaohongshu_timeout"}:
        return ("在浏览器里打开这条小红书笔记后点 Openmd 扩展保存，或从分享按钮复制完整链接再试；"
                "Agent 可把已加载的页面交给 openmd go <页面.html> --as-snapshot --url <链接>。")
    if error_code in {"douyin_asr_unavailable", "asr_model_missing"}:
        return "本机还没有语音模型：运行 openmd models install asr:baseline（约 1.6 GB，会先确认）后重试。"
    if error_code == "document_invalid_container":
        return "文件结构不完整或已损坏（常见于没下载完）：请重新下载或用原软件另存一份后再试。"
    if error_code == "douyin_detail_read_failed":
        return "抖音需要借用浏览器读取：确认 Chrome / Edge 已登录抖音、Openmd 扩展已加载（openmd browser-setup）后重试。"
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
