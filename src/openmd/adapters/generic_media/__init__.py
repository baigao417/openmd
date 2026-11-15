"""Generic media adapter factory.

Local file media is currently routed through the existing ``generic_media``
platform.  This module lets the registry discover a concrete local-audio
adapter without editing the shared registry.
"""

from __future__ import annotations

from openmd.adapters.local_audio import LocalAudioAdapter


def create_adapter(platform_id: str, default_backend: str) -> LocalAudioAdapter:
    return LocalAudioAdapter(platform_id, default_backend)
