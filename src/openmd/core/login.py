"""Shared login recovery metadata, without reading account/session material."""

from __future__ import annotations

from dataclasses import replace
import json
from urllib.parse import urlsplit

from .models import ContentItem, TranscriptResult


LOGIN_CODES = frozenset({
    "login_required", "auth_required", "login_wall", "not_logged_in", "session_expired",
    "backend_exit_77", "ai_chat_not_logged_in", "douyin_auth_or_browser_session_required",
    "weixin_exporter_auth_required", "weixin_exporter_session_expired",
    "xiaohongshu_browser_session_required", "session_auth_required",
})


def is_login_code(code: object) -> bool:
    value = str(code or "").lower()
    # Cloud API credentials (e.g. tingwu_auth_required) are not Chrome logins.
    site_prefixes = {"opencli", "weixin", "bilibili", "douyin", "xiaohongshu", "x", "xiaoe", "youtube", "discourse", "ai"}
    return value in LOGIN_CODES or (value.split("_", 1)[0] in site_prefixes and value.endswith(("_auth_required", "_not_logged_in", "_session_expired")))


def structured_login_code(stdout: str) -> str | None:
    """Read only the machine error code, never source prose or raw logs."""
    try:
        payload = json.loads(stdout)
        error = payload.get("error") if isinstance(payload, dict) and payload.get("ok") is False else None
        code = error.get("code") if isinstance(error, dict) else None
        return str(code) if is_login_code(code) else None
    except (ValueError, TypeError):
        return None


def login_for_url(url: str, platform: str | None = None) -> dict[str, str]:
    """Use declared destinations for known sites, otherwise the exact origin."""
    from openmd.routing.loader import load_catalog

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    records = load_catalog().capability_metadata
    for record in records:
        candidates = list((record.get("login_sites") or {}).values())
        if record.get("login_url"):
            candidates.append(record)
        for candidate in candidates:
            destination = str(candidate.get("login_url") or "")
            declared_host = urlsplit(destination).hostname or ""
            hosts = [declared_host, *candidate.get("login_hosts", [])]
            if host and any(host == h or host.endswith("." + h) for h in hosts if h):
                return {"site": str(candidate["display_name"]), "url": destination}
    # Keep only the origin: no path, signed query, userinfo, or fragment.
    origin = f"{parsed.scheme}://{host}" if parsed.scheme in {"http", "https"} and host else ""
    if origin and parsed.port:
        origin += f":{parsed.port}"
    return {"site": host or str(platform or "网站"), "url": origin}


def normalize_login_result(result: TranscriptResult, item: ContentItem | None) -> TranscriptResult:
    if result.status != "failed":
        return result
    original = result.error_code
    is_login = is_login_code(original)
    if result.metadata.get("exit_code") == 77 or (result.metadata.get("tool_run") or {}).get("exit_code") == 77:
        is_login = True
    # Legacy providers retain a structured OpenCLI error in the failure summary.
    # Never inspect successful source text, nor match arbitrary error prose.
    if not is_login:
        code = structured_login_code(result.error_summary or "")
        if code:
            original, is_login = code, True
    if not is_login:
        return result
    login = login_for_url(item.url, item.platform) if item else result.metadata.get("login", {})
    return replace(
        result, text="", blocks=[], components=[], source_level="partial_extraction",
        error_code="login_required", error_summary=login_hint(login),
        metadata={**result.metadata, "login": login,
                  "original_error_code": result.metadata.get("original_error_code") or original,
                  "failure_class": "user_resolvable"},
    )


def login_hint(login: dict) -> str:
    return f"需要登录 {login.get('site') or '该网站'}：请在 Chrome 打开 {login.get('url') or '网站首页'} 登录后重试。"


def login_action(login: dict) -> dict[str, str]:
    return {"type": "login", "site": str(login.get("site") or ""), "url": str(login.get("url") or "")}


def prompt_login_retry(login: dict) -> bool:
    """Caller owns the tty/JSON guard and one-retry limit."""
    import webbrowser

    answer = input("按回车在浏览器打开登录页（n 取消）：")
    if answer.strip().lower() in {"n", "no", "取消"}:
        return False
    webbrowser.open(login["url"])
    answer = input("登录完成后再按回车重试一次（n 取消）：")
    return answer.strip().lower() not in {"n", "no", "取消"}
