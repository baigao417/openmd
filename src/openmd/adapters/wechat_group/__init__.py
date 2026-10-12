"""Wechat group conversation adapter factory."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import WechatGroupAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return WechatGroupAdapter(platform_id, default_backend)
