from __future__ import annotations

from openmd.adapters.registry import DEFAULT_BACKENDS
from openmd.core.capabilities import APPROVED_CAPABILITIES, get_capability


def test_capability_planned_provider_comes_from_current_registry_or_explicit_local_route() -> None:
    registry_sources = {
        "bilibili": "bilibili",
        "youtube": "youtube",
        "weixin": "weixin",
        "xiaoyuzhou": "xiaoyuzhou",
        "douyin": "douyin",
        "tiktok": "tiktok",
        "xiaohongshu": "xiaohongshu",
        "xiaohongshu_creator": "xiaohongshu",
        "xiaoe": "xiaoe",
        "x": "x",
        "web_article": "web_article",
        "feishu": "feishu",
        "generic_media": "generic_media",
        "pdf_office": "pdf",
        "screenshot": "screenshot",
        "wechat_moments": "wechat_moments",
        "wechat_group": "wechat_group",
        "qq_group": "qq_group",
    }
    for source_id, platform in registry_sources.items():
        record = get_capability(source_id)
        assert record.planned_provider == DEFAULT_BACKENDS[platform][0]

    assert get_capability("local_audio").planned_provider == "asr.faster_whisper"
    assert get_capability("local_video").planned_provider == "asr.fallback"
    assert get_capability("tutorial_video").planned_provider == "asr.fallback"


def test_every_capability_explicitly_separates_planning_and_runtime_binding() -> None:
    for record in APPROVED_CAPABILITIES:
        assert record.binding_state in {"implemented", "unimplemented"}
        if record.binding_state == "unimplemented":
            assert record.runtime_provider is None
            assert not record.execution_ready
        else:
            assert record.runtime_provider is not None
