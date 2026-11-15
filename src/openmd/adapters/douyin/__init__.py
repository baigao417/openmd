"""Douyin V1 platform adapter."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import DouyinAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return DouyinAdapter(platform_id, default_backend)
