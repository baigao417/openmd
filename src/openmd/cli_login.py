"""Login recovery for single and batch CLI extraction."""

from __future__ import annotations

import sys

from openmd.core.login import login_action, login_for_url, login_hint, normalize_login_result, prompt_login_retry


def paused_login(args, url):
    return getattr(args, "_login_paused", {}).get(login_for_url(url)["url"])


def recovery_fields(args):
    values = list(getattr(args, "_login_paused", {}).values())
    if not values:
        return {}
    login = values[0]
    return {"error_code": "login_required", "login": login, "next_action": login_action(login)}


def source_next_action(error_code: str | None) -> str | None:
    """Return a bounded user route for source-level blocks."""
    if error_code in {"xiaohongshu_request_blocked", "xiaohongshu_loaded_page_mismatch"}:
        return "在浏览器打开这篇小红书笔记后，点击 Openmd 扩展保存；Openmd 将读取当前已加载页面。"
    return None


def extract_with_login(executor, item, chain, args):
    from openmd.interactive import is_interactive

    result = normalize_login_result(executor.extract(item, chain, allow_fallback=args.fallback), item)
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
