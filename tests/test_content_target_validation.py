"""Requested body/transcript validation at the backend fallback boundary."""

from dataclasses import replace

import pytest

from openmd.backends.executor import BackendExecutor
from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult


def result(text="Original source", **metadata):
    return TranscriptResult(
        text=text, status="verified", source_level="native_text", backend="first",
        metadata=metadata,
        components=[ExtractionComponent(id="content", kind="text", status="verified", source_level="native_text", backend="first")],
    )


class Provider:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def extract(self, request):
        self.calls += 1
        return replace(self.value, backend=request.primary_backend)


@pytest.mark.parametrize("first", [
    result("# A video\n\nSource: fixture", metadata_only=True),
    result("Source only", metadata_only=True, terminal_partial=True),
    replace(result("metadata", metadata_only=True, terminal_partial=True), status="partial"),
    replace(result("", terminal_partial=True), status="partial"),
    result("# Forum\n\n## First post\nRecommended topics", engine="discourse"),
    result("# Page header", page_shell=True),
    result("Just the description", content_kind="description"),
])
def test_unusable_content_never_stops_fallback_even_if_terminal(first):
    fallback = Provider(result("Actual body", engine="discourse", post_id=123,
                               target_post_id=123, post_body_present=True))
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test/topic")
    extracted = BackendExecutor(providers={"first": Provider(first), "fallback": fallback}).extract(item, ("first", "fallback"))
    assert extracted.backend == "fallback"
    assert extracted.text == "Actual body"
    assert fallback.calls == 1
    failed = validate_extraction_content(first, item)
    assert failed.status == "failed"
    assert failed.text == ""
    assert not failed.components
    assert not failed.has_usable_text
    assert "terminal_partial" not in failed.metadata


def test_metadata_is_only_accepted_for_an_explicit_metadata_target():
    item = ContentItem(platform="douyin", input_type="single", source_type="video", id="123", url="https://example.test/123")
    metadata = result("# Source\n\nVideo ID: 123", metadata_only=True)
    executor = BackendExecutor(providers={"douyin.metadata": Provider(metadata)})
    assert executor.extract(item, ("douyin.metadata",)).error_code == "content_metadata_only"
    explicit = replace(item, raw_meta={"extraction_target": "metadata"})
    assert executor.extract(explicit, ("douyin.metadata",)).status == "verified"


@pytest.mark.parametrize("body", ["好", "回复", "分享这个结果。", "[参考](https://example.test)"])
def test_short_discourse_post_is_accepted_with_structural_evidence(body):
    extracted = validate_extraction_content(result(body, engine="discourse", post_id=123, target_post_id=123, post_body_present=True))
    assert extracted.status == "verified"
    assert extracted.text == body


@pytest.mark.parametrize("metadata", [
    {"post_id": 123, "target_post_id": 124, "post_body_present": True},
    {"post_id": 123, "target_post_id": 123, "post_body_present": False},
    {"post_id": 123, "post_body_present": True},
])
def test_discourse_requires_the_requested_post_id_and_actual_body(metadata):
    extracted = validate_extraction_content(result("Forum headings and suggested topics", engine="discourse", **metadata))
    assert extracted.error_code == "discourse_post_body_missing"


@pytest.mark.parametrize("source_level", ["native_caption", "auto_caption", "platform_transcript", "media_asr", "asr", "ocr"])
def test_douyin_transcript_sources_remain_distinct_from_status(source_level):
    item = ContentItem(platform="douyin", input_type="single", source_type="video", url="https://example.test/video")
    extracted = validate_extraction_content(replace(result("真实语音"), source_level=source_level), item)
    assert extracted.status == "verified"
    assert extracted.source_level == source_level


def test_video_description_cannot_claim_to_be_a_transcript():
    item = ContentItem(platform="douyin", input_type="single", source_type="video", url="https://example.test/video")
    assert validate_extraction_content(result("A video description"), item).error_code == "transcript_not_produced"


def test_explicit_transcript_target_checks_any_video_platform():
    assert validate_extraction_content(result("A title"), target="transcript").error_code == "transcript_not_produced"


def test_detail_confirmed_image_post_accepts_author_body_for_content_only():
    item = ContentItem(platform="douyin", input_type="single", source_type="video", url="https://www.douyin.com/video/123")
    author = replace(result("作者图文正文", media_kind="image_text", transcript_method="image_post_text", content_kind="author_text"), backend="douyin.post-text")
    assert validate_extraction_content(author, item).status == "verified"
    assert validate_extraction_content(author, item, target="transcript").error_code == "transcript_not_produced"
    assert validate_extraction_content(replace(author, backend="generic"), item).error_code == "transcript_not_produced"
    assert validate_extraction_content(replace(author, metadata={"transcript_method": "image_post_text"}), item).error_code == "transcript_not_produced"


def test_discovered_engine_requires_structural_body_from_later_generic_backends():
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test/topic", raw_meta={"site_engine": "discourse"})
    assert validate_extraction_content(result("Forum shell"), item).error_code == "discourse_post_body_missing"


def test_engine_detected_by_failed_provider_is_retained_across_fallbacks():
    detected = replace(result("", engine="discourse"), status="failed", error_code="discourse_login_required")
    generic = Provider(result("Forum headings and recommendations"))
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test/t/post/1")
    extracted = BackendExecutor(providers={"engine": Provider(detected), "generic": generic}).extract(item, ("engine", "generic"))
    assert extracted.status == "failed"
    assert extracted.error_code == "discourse_post_body_missing"
    assert generic.calls == 1
    assert "discourse_login_required" in " ".join(extracted.source_notes)
    assert not item.raw_meta  # Detection does not mutate the caller's manifest item.
