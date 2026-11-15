"""Static collection permissions shared by planning and CLI execution.

This module must not construct providers or inspect credentials/session state.
An approved product goal is not permission to run an unsupported operation.
"""

from __future__ import annotations

from openmd.adapters.wechat_group.wx_cli import OPENCLI_WX_HISTORY_DISCOVERY_BACKEND
from openmd.adapters.wechat_moments.wx_cli import OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND
from openmd.adapters.weixin.discovery import WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND
from openmd.backends.bilibili import BILIBILI_NATIVE_DISCOVERY_BACKEND
from openmd.backends.douyin import DOUYIN_OPENCLI_USER_VIDEOS_BACKEND, DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND
from openmd.backends.xiaoe import OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND
from openmd.backends.xiaohongshu import (
    OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
    OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND,
)
from openmd.backends.xiaoyuzhou import OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND
from openmd.backends.youtube_discovery import (
    YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
)


SCOPED_ALL_PROVIDERS: dict[str, frozenset[str | None]] = {
    "weixin": frozenset({None, WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND}),
    "douyin": frozenset({None, DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND}),
    "xiaohongshu": frozenset({None, OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND}),
}
_YOUTUBE_ALL_PROVIDERS = frozenset(
    {None, YOUTUBE_DATA_API_DISCOVERY_BACKEND, YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND}
)


def collect_all_allowed(
    platform: str,
    *,
    browser_enabled: bool,
    network_enabled: bool = False,
    discovery_provider: str | None = None,
) -> bool:
    """Match current scoped CLI support; callers normalize provider aliases."""
    if platform == "youtube":
        return network_enabled and discovery_provider in _YOUTUBE_ALL_PROVIDERS
    return browser_enabled and discovery_provider in SCOPED_ALL_PROVIDERS.get(platform, frozenset())


# These are discovery IDs, deliberately separate from extraction bindings.
COLLECTION_BINDINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bilibili": (BILIBILI_NATIVE_DISCOVERY_BACKEND, ("network",)),
    "youtube": (YOUTUBE_DATA_API_DISCOVERY_BACKEND, ("network", "account")),
    "weixin": (WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND, ("browser",)),
    "xiaoyuzhou": (OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND, ("browser",)),
    "douyin": (DOUYIN_OPENCLI_USER_VIDEOS_BACKEND, ("browser",)),
    "xiaoe": (OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND, ("browser",)),
    "xiaohongshu_creator": (OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND, ("browser",)),
    "wechat_group": (OPENCLI_WX_HISTORY_DISCOVERY_BACKEND, ("browser",)),
    "wechat_moments": (OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND, ("browser",)),
}
