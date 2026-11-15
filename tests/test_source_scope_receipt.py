from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.runlog import build_item_runlog_entry
from openmd.core.secrets import sanitize_runlog_entry, sanitize_source_scope


def test_scope_receipt_roundtrip_keeps_identity_and_counts_only():
    item = ContentItem(platform="web_article", input_type="single", url="https://forum.example/t/topic/1")
    result = TranscriptResult(text="原始帖子", status="verified", source_level="native_text", backend="web_article.discourse",
                              metadata={"engine": "discourse", "topic_id": 1, "post_id": 2,
                                        "target_post_id": 2, "post_body_present": True, "scope": "all_posts",
                                        "coverage": {"expected_count": 32, "fetched_count": 31, "missing_count": 1,
                                                     "complete": False, "text": "private source", "play_url": "https://media.example/signed"}})
    scope = sanitize_runlog_entry(build_item_runlog_entry(item, result))["source_scope"]
    assert scope == {"engine": "discourse", "topic_id": 1, "post_id": 2, "target_post_id": 2,
                     "post_body_present": True, "scope": "all_posts",
                     "coverage": {"expected_count": 32, "fetched_count": 31, "missing_count": 1, "complete": False}}


def test_scope_drops_arbitrary_labels_and_invalid_metrics():
    assert sanitize_source_scope({"engine": "https://private.example", "topic_id": True,
                                  "post_id": "not-id", "scope": "raw source text",
                                  "coverage": {"fetched_count": -1, "duration_seconds": float("inf")}}) == {}
