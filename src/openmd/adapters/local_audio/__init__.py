"""Local audio adapter lane.

The detector currently routes media-like inputs to ``generic_media``.  The
``generic_media`` adapter module reuses this local-audio adapter for local files
without requiring platform lanes to edit the shared registry.
"""

from __future__ import annotations

from .adapter import LocalAudioAdapter


def create_adapter(platform_id: str, default_backend: str) -> LocalAudioAdapter:
    return LocalAudioAdapter(platform_id, default_backend)
