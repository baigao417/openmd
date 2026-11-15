"""Bounded detail and local ASR regression tests; no real network or models."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from openmd.adapters.douyin.adapter import DouyinAdapter
from openmd.backends.base import BackendExtractRequest
from openmd.backends.douyin_detail import DouyinDetailError, DouyinDetailProvider, douyin_video_id
from openmd.backends.douyin_providers import DouyinTranscriptProvider
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.detector import detect_platform
from openmd.core.models import TranscriptResult


VIDEO_ID = "7706182298391153954"
URL = "https://www.douyin.com/video/" + VIDEO_ID
OPTIONS = BackendRuntimeOptions(enable_browser_backends=True, enable_media_download_backends=True, enable_asr_backends=True)


def item(**raw_meta):
    value = DouyinAdapter().resolve_items(URL)[0]
    return replace(value, raw_meta={**value.raw_meta, **raw_meta})


def detail(payload=None, calls=None):
    payload = payload or {"aweme_id": VIDEO_ID, "desc": "作品文案", "author": "作者", "duration_s": 68, "play_url": "https://media.example/video"}
    def runner(command, *, timeout):
        if calls is not None:
            calls.append(command)
        return 0, json.dumps({"ok": True, "data": payload}), ""
    return DouyinDetailProvider(runner=runner)


def runner(command, timeout):
    Path(command[-1]).write_bytes(b"audio")
    return 0, "", ""


class LocalAsr:
    name = "asr.auto"
    def __init__(self, value=None, available=True):
        self.paths = []
        self.available = available
        self.value = value or TranscriptResult(text="[00:00:01] 这是开头\n[00:01:07] 这是结尾\n", status="verified", source_level="asr", backend="asr.qwen3", metadata={"asr_model": "existing-qwen", "asr_route": {"selected_backend": "asr.qwen3", "detected_language": "zh"}})
    def is_available(self):
        return self.available
    def transcribe(self, path):
        assert path.is_file()
        self.paths.append(path)
        return self.value


@pytest.mark.parametrize("target", [VIDEO_ID, URL, "https://www.douyin.com/user/author?modal_id=" + VIDEO_ID, "https://www.douyin.com/note/" + VIDEO_ID])
def test_single_video_normalization(target):
    assert douyin_video_id(target) == VIDEO_ID
    assert detect_platform(target) == "douyin"
    resolved = DouyinAdapter().resolve_items(target)
    assert len(resolved) == 1
    assert resolved[0].id == VIDEO_ID and resolved[0].input_type == "single"


def test_share_link_is_single_item_and_resolves_before_extraction():
    share = DouyinAdapter().resolve_items("复制打开抖音 https://v.douyin.com/fixture/")[0]
    assert share.input_type == "single"
    calls = []
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(calls=calls), runner=runner, asr_backend=LocalAsr())
    identity = provider.resolve_item_identity(share)
    assert identity.ok and identity.item.id == VIDEO_ID
    result = provider.extract(BackendExtractRequest(identity.item, (provider.name,)))
    assert result.has_usable_text
    assert len(calls) == 1
    assert calls[0] == ["opencli", "douyin", "detail", "https://v.douyin.com/fixture/", "--window", "background", "--keep-tab", "false", "-f", "json"]


def test_detail_rejects_wrong_id_and_never_retains_raw_response():
    with pytest.raises(DouyinDetailError) as caught:
        detail({"aweme_id": "1234567890", "play_url": "https://media.example/video"}).resolve(item())
    assert caught.value.code == "douyin_detail_id_mismatch"


@pytest.mark.parametrize("fields,author,published", [
    ({"author": {"nickname": "昵称"}, "create_time": 1794182400}, "昵称", "2026-11-09T00:00:00+00:00"),
    ({"nickname": "顶层昵称", "create_time": "1794182400000"}, "顶层昵称", "2026-11-09T00:00:00+00:00"),
    ({"user": {"nickname": "用户"}, "created": "2026-11-09 14:43:21"}, "用户", "2026-11-09 14:43:21"),
])
def test_detail_metadata_normalization_matches_discovery(fields, author, published):
    value = detail({"aweme_id": VIDEO_ID, "desc": "作品标题", **fields}).resolve(item())
    assert value.author == author and value.published_at == published


def test_detail_preserves_existing_correct_metadata():
    original = replace(item(), title="原始标题  ", author="已知作者", published_at="2026-11-08")
    value = detail({"aweme_id": VIDEO_ID, "desc": "另一个显示标题", "author": "详情作者", "created": "2026-11-09"}).resolve(original)
    assert (value.title, value.author, value.published_at) == (original.title, original.author, original.published_at)


def test_default_uses_existing_local_runtime_and_removes_temporary_audio():
    local = LocalAsr()
    with patch("openmd.runtime.asr.get_asr_backend", return_value=local) as factory:
        provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(), runner=runner)
        value = provider.extract(BackendExtractRequest(item(), (provider.name,)))
    factory.assert_called_once_with("asr.auto")
    assert value.backend == "asr.qwen3"
    assert value.metadata["asr_model"] == "existing-qwen"
    assert value.metadata["asr_route"]["selected_backend"] == "asr.qwen3"
    assert value.title == "作品文案" and value.author == "作者"
    assert value.metadata["coverage"]["duration_seconds"] == 68
    assert all(not path.exists() for path in local.paths)
    assert "media.example" not in str(value)
    assert "作品文案" not in value.text


def test_native_timing_and_repeated_speech_are_preserved():
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    value = provider.extract(BackendExtractRequest(item(transcript_candidates=[{"text": "重复", "start_ms": 1000}, {"text": "重复", "start": 9}, {"text": "[00:00:19] 结尾"}]), (provider.name,)))
    assert value.text == "[00:00:01] 重复\n[00:00:09] 重复\n[00:00:19] 结尾\n"


def test_untimed_native_captions_do_not_get_invented_zero_timestamps():
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    value = provider.extract(BackendExtractRequest(item(subtitles=[{"text": "重复"}, {"text": "重复"}]), (provider.name,)))
    assert value.text == "重复\n重复\n"


def test_caption_download_locator_is_not_a_transcript_body():
    local = LocalAsr()
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(), runner=runner, asr_backend=local)
    value = provider.extract(BackendExtractRequest(item(subtitles="https://captions.example/track.vtt"), (provider.name,)))
    assert value.backend == "asr.qwen3"
    assert "captions.example" not in value.text


def test_no_speech_is_failure_without_description_fallback():
    local = LocalAsr(TranscriptResult(text="", status="failed", source_level="asr", backend="asr.qwen3", error_code="asr_empty"))
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(), runner=runner, asr_backend=local)
    value = provider.extract(BackendExtractRequest(item(), (provider.name,)))
    assert value.error_code == "douyin_no_speech"
    assert not value.has_usable_text
    assert all(not path.exists() for path in local.paths)


def test_image_detail_preserves_author_text_without_media_asr_or_ocr():
    body = "广西加油。\n\n[00:02] 作者写的时间\n重复\n重复  \n"
    calls = []
    local = LocalAsr()
    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_browser_backends=True),
        detail_provider=detail({"aweme_id": VIDEO_ID, "desc": body, "images": [{"url": "https://images.example/1"}]}, calls),
        runner=lambda *_: pytest.fail("image posts must not download media"),
        asr_backend=local,
        ocr_extractor=lambda _: pytest.fail("image author text must not use OCR"),
    )
    with patch("openmd.runtime.asr.get_asr_backend", side_effect=AssertionError("ASR must not initialize")):
        value = provider.extract(BackendExtractRequest(item(), (provider.name,)))
    assert len(calls) == 1
    assert value.text == body
    assert value.backend == "douyin.post-text" and value.source_level == "native_text"
    assert value.metadata["transcript_method"] == "image_post_text"
    assert value.metadata["author_text_source"] == "detail_desc"
    assert value.metadata["content_kind"] == "author_text"
    assert value.metadata["media_kind"] == "image_text"
    assert not local.paths


def test_known_image_without_author_text_reads_bounded_detail_even_with_audio_url():
    calls = []
    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_browser_backends=True),
        detail_provider=detail({"aweme_id": VIDEO_ID, "desc": "作者正文\n第二段", "media_kind": "image_text"}, calls),
    )
    original = replace(item(play_url="https://media.example/audio"), source_type="image_text", title=VIDEO_ID)
    value = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert value.text == "作者正文\n第二段"
    assert len(calls) == 1


@pytest.mark.parametrize("description", [None, "", "  \n"])
def test_empty_image_detail_never_substitutes_display_title(description):
    calls = []
    payload = {"aweme_id": VIDEO_ID, "title": "显示标题不是作者正文", "aweme_type": 68}
    if description is not None:
        payload["desc"] = description
    resolver = detail(payload, calls)
    original = resolver.resolve(item())
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=resolver)
    value = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert value.error_code == "douyin_image_post_text_unavailable"
    assert not value.has_usable_text and value.text == ""
    assert len(calls) == 1  # Already-read empty detail does not refetch.


@pytest.mark.parametrize("title", [VIDEO_ID, "无标题", "untitled", "Douyin video " + VIDEO_ID, URL])
def test_known_image_placeholder_title_never_becomes_body(title):
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    original = replace(item(), source_type="image_text", title=title)
    value = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert value.error_code == "douyin_image_post_text_unavailable"
    assert value.text == ""


def test_explicit_image_author_text_overrides_display_title_verbatim():
    body = "[00:01] 自己写的时间\n[00:02] 自己写的时间\n\n最后一段  \n"
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    original = replace(item(author_text=body), source_type="image_text", title="独立显示标题")
    value = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert value.text == body
    assert value.metadata["author_text_source"] == "explicit_author_text"


def test_video_description_and_explicit_author_text_are_not_a_video_transcript():
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    value = provider.extract(BackendExtractRequest(item(author_text="作品文案", play_url="https://media.example/video"), (provider.name,)))
    assert not value.has_usable_text
    assert value.error_code == "douyin_media_asr_not_enabled"


def test_unavailable_local_asr_does_not_download_media_or_open_cloud():
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(), runner=lambda *_: pytest.fail("media must not run"), asr_backend=LocalAsr(available=False))
    value = provider.extract(BackendExtractRequest(item(), (provider.name,)))
    assert value.error_code == "douyin_asr_unavailable"


def test_expired_media_error_does_not_expose_full_signed_url():
    sensitive_url = "https://media.example/video?" + "signature=" + "private-signature"
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(), runner=lambda *args: (1, "", sensitive_url), asr_backend=LocalAsr())
    value = provider.extract(BackendExtractRequest(item(play_url=sensitive_url), (provider.name,)))
    assert value.error_code == "douyin_media_url_unavailable"
    assert sensitive_url not in str(value)
    assert "private-signature" not in str(value)


def test_detail_failure_is_structured_and_does_not_persist_stdout_or_stderr():
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=DouyinDetailProvider(runner=lambda *a, **k: (1, "raw response", "raw diagnostics")))
    value = provider.extract(BackendExtractRequest(item(), (provider.name,)))
    assert value.error_code == "douyin_detail_read_failed"
    assert "raw diagnostics" not in str(value)


def test_explicit_metadata_backend_survives_cli_target_preparation(tmp_path):
    import argparse
    from openmd.cli import _prepare_resolved_item
    args = argparse.Namespace(backend="douyin.metadata", receipt=None, package=False)
    prepared, _ = _prepare_resolved_item(args, item(), item(), ("douyin.metadata",))
    assert prepared.raw_meta["extraction_target"] == "metadata"


def test_cloud_registration_is_only_explicit_with_all_permissions():
    from openmd.backends.registry import create_runtime_backend_provider_registry
    from openmd.backends.douyin_providers import DouyinCloudTranscriptProvider
    requested = "siliconflow.sensevoice"
    assert not isinstance(create_runtime_backend_provider_registry("douyin", OPTIONS).provider_for(requested), DouyinCloudTranscriptProvider)
    closed = replace(OPTIONS, backend_override=requested)
    assert not isinstance(create_runtime_backend_provider_registry("douyin", closed).provider_for(requested), DouyinCloudTranscriptProvider)
    opened = replace(closed, enable_cloud_backends=True)
    assert isinstance(create_runtime_backend_provider_registry("douyin", opened).provider_for(requested), DouyinCloudTranscriptProvider)
    # Merely opening a cloud permission cannot make it the default provider.
    unselected = replace(OPTIONS, enable_cloud_backends=True)
    assert not isinstance(create_runtime_backend_provider_registry("douyin", unselected).provider_for(requested), DouyinCloudTranscriptProvider)
