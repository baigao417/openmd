"""Bilibili discovery lane helpers.

The real providers live in ``openmd.backends.bilibili`` and are registered only
behind explicit network/browser gates. This module keeps adapter-lane imports
stable without duplicating provider logic.
"""

from __future__ import annotations

from openmd.backends.bilibili import (
    BILIBILI_NATIVE_DISCOVERY_BACKEND,
    BILIBILI_OPENCLI_DISCOVERY_BACKEND,
    BilibiliNativeUserVideosProvider,
    OpenCliBilibiliUserVideosProvider,
    extract_bilibili_uid,
)


def build_opencli_bilibili_user_videos_args(selector: str, *, limit: int | None) -> list[str]:
    uid = extract_bilibili_uid(selector) or selector.strip()
    command = ["opencli", "bilibili", "user-videos", uid]
    if limit is not None:
        command.extend(["--limit", str(max(0, limit))])
    return command


__all__ = [
    "BILIBILI_NATIVE_DISCOVERY_BACKEND",
    "BILIBILI_OPENCLI_DISCOVERY_BACKEND",
    "BilibiliNativeUserVideosProvider",
    "OpenCliBilibiliUserVideosProvider",
    "build_opencli_bilibili_user_videos_args",
]
