"""QQ group conversation adapter factory."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import QQGroupAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return QQGroupAdapter(platform_id, default_backend)
