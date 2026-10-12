"""Weixin/WeChat Official Account V1 platform adapter."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import WeixinOfficialAccountAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return WeixinOfficialAccountAdapter(platform_id, default_backend)
