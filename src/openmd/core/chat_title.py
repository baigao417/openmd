"""Shared title policy for loaded pages and experimental detail exports."""
from __future__ import annotations

import re
from collections.abc import Sequence

from openmd.core.models import TurnBlock


SITE_TITLES = {
    "chatgpt": ("ChatGPT", "OpenAI ChatGPT"),
    "grok": ("Grok",),
    "deepseek": ("DeepSeek", "DeepSeek - 深度求索"),
    "doubao": ("豆包", "Doubao"),
    "gemini": ("Google Gemini", "Gemini"),
    "qwen": ("千问-阿里 AI 助手", "千问", "通义千问", "Qwen Chat", "Qwen"),
    "yuanbao": ("腾讯元宝", "元宝", "Yuanbao"),
}


def clean_chat_title(site: str, title: str | None) -> str | None:
    value = re.sub(r"\s+", " ", title or "").strip()
    aliases = SITE_TITLES.get(site, (site,))
    pattern = "(?:" + "|".join(re.escape(alias).replace(r"\ ", r"\s*").replace(r"\-", r"\s*[-–—]\s*") for alias in aliases) + ")"
    if re.fullmatch(pattern, value, re.I):
        return None
    value = re.sub(r"\s*[-–—|·]\s*" + pattern + r"\s*$", "", value, flags=re.I).strip()
    return value or None


def conversation_title(site: str, page_title: str | None, turns: Sequence[TurnBlock], *, session_title: str | None = None) -> str:
    """Keep meaningful page titles, then current session title, then first user."""
    for candidate in (page_title, session_title):
        title = clean_chat_title(site, candidate)
        if title:
            return title
    for turn in turns:
        if turn.role.lower() == "user" and turn.text.strip():
            text = re.sub(r"\s+", " ", turn.text).strip()
            return text[:30] + ("…" if len(text) > 30 else "")
    return f"{site} conversation"
