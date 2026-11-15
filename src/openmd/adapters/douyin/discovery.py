"""Douyin discovery lane helpers.

The real bounded OpenCLI discovery provider lives in ``openmd.backends.douyin``
and is registered only behind the explicit browser backend gate.
"""

from __future__ import annotations

from openmd.backends.douyin import (
    DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND,
    DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
    OpenCliOpenmdDouyinCreatorVideosProvider,
    OpenCliDouyinUserVideosProvider,
    build_opencli_openmd_douyin_creator_videos_args,
    build_opencli_douyin_user_videos_args,
    classify_douyin_input_failure,
    douyin_user_target,
    normalize_douyin_collection_url,
    parse_opencli_douyin_user_videos,
    parse_opencli_openmd_douyin_creator_videos,
)


__all__ = [
    "DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND",
    "DOUYIN_OPENCLI_USER_VIDEOS_BACKEND",
    "OpenCliOpenmdDouyinCreatorVideosProvider",
    "OpenCliDouyinUserVideosProvider",
    "build_opencli_openmd_douyin_creator_videos_args",
    "build_opencli_douyin_user_videos_args",
    "classify_douyin_input_failure",
    "douyin_user_target",
    "normalize_douyin_collection_url",
    "parse_opencli_douyin_user_videos",
    "parse_opencli_openmd_douyin_creator_videos",
]
