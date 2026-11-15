from __future__ import annotations

from pathlib import Path

from openmd.backends.base import BackendExtractRequest
from openmd.backends.douyin_providers import (
    DOUYIN_ASR_BACKEND,
    DOUYIN_NATIVE_TRANSCRIPT_BACKEND,
    DOUYIN_OCR_BACKEND,
    DOUYIN_POST_TEXT_BACKEND,
    DouyinTranscriptProvider,
)
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem


def _item(*, source_type: str = "video", title: str = "Fixture", **raw_meta: object) -> ContentItem:
    return ContentItem(
        platform="douyin",
        input_type="collection",
        url="https://www.douyin.com/video/7123456789012345678",
        id="7123456789012345678",
        title=title,
        author="Creator",
        source_type=source_type,  # type: ignore[arg-type]
        raw_meta=dict(raw_meta),
    )


class FakeAsr:
    name = DOUYIN_ASR_BACKEND

    def __init__(self, text: str = "识别成功。", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.paths: list[Path] = []

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path: Path, *, timeout: int) -> str:
        self.paths.append(audio_path)
        if self.error:
            raise self.error
        assert timeout > 0
        assert audio_path.exists()
        return self.text


def test_native_candidates_win_without_media_or_asr() -> None:
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    result = provider.extract(
        BackendExtractRequest(
            item=_item(transcript_candidates=[{"source": "video_text", "text": "第一句\n第二句"}]),
            backend_chain=(provider.name,),
            allow_fallback=False,
        )
    )
    assert result.has_usable_text
    assert result.backend == DOUYIN_NATIVE_TRANSCRIPT_BACKEND
    assert result.source_level == "platform_transcript"
    assert "第一句" in result.text


def test_media_fallback_requires_explicit_gates() -> None:
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    result = provider.extract(
        BackendExtractRequest(
            item=_item(play_url="https://media.example/video"),
            backend_chain=(provider.name,),
            allow_fallback=False,
        )
    )
    assert not result.has_usable_text
    assert result.error_code == "douyin_media_asr_not_enabled"
    assert "media.example" not in (result.error_summary or "")


def test_asr_uses_temp_audio_and_does_not_return_play_url(tmp_path: Path) -> None:
    asr = FakeAsr("这是逐字稿。")

    def runner(command: list[str], timeout: int) -> tuple[int, str, str]:
        assert timeout > 0
        output = Path(command[-1])
        output.write_bytes(b"audio")
        return 0, "", ""

    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_media_download_backends=True, enable_asr_backends=True, enable_cloud_backends=True),
        runner=runner,
        asr_client=asr,
    )
    result = provider.extract(
        BackendExtractRequest(
            item=_item(play_url="https://media.example/video?token=secret"),
            backend_chain=(provider.name,),
            allow_fallback=False,
        )
    )
    assert result.has_usable_text
    assert result.backend == DOUYIN_ASR_BACKEND
    assert result.source_level == "media_asr"
    assert all(not path.exists() for path in asr.paths)
    assert "media.example" not in result.text
    assert "token=secret" not in str(result.metadata)


def test_ocr_recovers_when_asr_fails() -> None:
    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_media_download_backends=True, enable_asr_backends=True, enable_cloud_backends=True),
        runner=lambda command, timeout: (0, "", ""),
        asr_client=FakeAsr(error=RuntimeError("no speech")),
        ocr_extractor=lambda item: "[00:02] 屏幕硬字幕",
    )

    def runner(command: list[str], timeout: int) -> tuple[int, str, str]:
        Path(command[-1]).write_bytes(b"audio")
        return 0, "", ""

    provider._runner = runner
    result = provider.extract(
        BackendExtractRequest(
            item=_item(play_url="https://media.example/video"),
            backend_chain=(provider.name,),
            allow_fallback=False,
        )
    )
    assert result.has_usable_text
    assert result.backend == DOUYIN_OCR_BACKEND
    assert result.source_level == "ocr"
    assert "屏幕硬字幕" in result.text


def test_image_post_preserves_legacy_author_description_without_media_or_asr() -> None:
    def runner(command: list[str], timeout: int) -> tuple[int, str, str]:
        raise AssertionError("image author text must not download media")

    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(),
        runner=runner,
        asr_client=FakeAsr(error=RuntimeError("SiliconFlow ASR returned no meaningful text")),
        ocr_extractor=lambda _item: (_ for _ in ()).throw(AssertionError("frame OCR must not run for audio-only image posts")),
    )
    result = provider.extract(
        BackendExtractRequest(
            item=_item(
                source_type="image_text",
                title="广西加油。#广西 #广西洪水",
                play_url="https://media.example/audio",
            ),
            backend_chain=(provider.name,),
            allow_fallback=False,
        )
    )

    assert result.has_usable_text
    assert result.backend == DOUYIN_POST_TEXT_BACKEND
    assert result.source_level == "native_text"
    assert result.text == "广西加油。#广西 #广西洪水"
    assert result.metadata["transcript_method"] == "image_post_text"
    assert result.metadata["author_text_source"] == "legacy_image_title"


def test_short_image_post_author_text_needs_no_transcript_length_threshold() -> None:
    provider = DouyinTranscriptProvider(BackendRuntimeOptions())
    result = provider.extract(BackendExtractRequest(_item(source_type="image_text", title="好"), (provider.name,)))
    assert result.text == "好"
    assert result.source_level == "native_text"
