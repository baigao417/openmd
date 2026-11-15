"""Wechat Moments conversation adapter factory."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import WechatMomentsAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return WechatMomentsAdapter(platform_id, default_backend)
