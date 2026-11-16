"""OpenCLI 2.x wraps JSON in {ok, data}; every parser must accept both shapes."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from openmd.backends.bilibili import parse_bilibili_subtitle_segments, parse_opencli_bilibili_user_videos
from openmd.backends.douyin import parse_opencli_douyin_user_videos
from openmd.backends.xiaoe import parse_xiaoe_content_result
from openmd.backends.xiaohongshu import parse_opencli_xiaohongshu_note_output
from openmd.backends.xiaoyuzhou import parse_xiaoyuzhou_episode_items
from openmd.backends.youtube import _extract_transcript_text
from openmd.backends.youtube_discovery import parse_opencli_youtube_channel_json, parse_opencli_youtube_playlist_output
from openmd.core.opencli_output import load_opencli_json, unwrap_opencli_envelope


def shapes(rows):
    return [json.dumps(rows), json.dumps({"ok": True, "data": rows})]


def test_unwrap_only_touches_real_envelopes():
    assert unwrap_opencli_envelope({"ok": True, "data": [1]}) == [1]
    failure = {"ok": False, "error": {"code": "unknown_command", "message": "x"}}
    assert unwrap_opencli_envelope(failure) == failure
    # A platform record that merely has these field names is not an envelope.
    record = {"ok": True, "data": [1], "title": "keep me"}
    assert unwrap_opencli_envelope(record) == record
    assert unwrap_opencli_envelope([{"ok": True}]) == [{"ok": True}]
    with pytest.raises(json.JSONDecodeError):
        load_opencli_json("not json")


@pytest.mark.parametrize("stdout", shapes([{"text": "hello"}]))
def test_youtube_transcript(stdout):
    assert _extract_transcript_text(stdout, output_format="json") == "hello"


def test_youtube_failure_envelope_is_not_transcript_text():
    stdout = json.dumps({"ok": False, "error": {"code": "x", "message": "secret-free message"}})
    assert _extract_transcript_text(stdout, output_format="json") == ""


CHANNEL = [{"field": "Title", "value": "t", "url": "https://www.youtube.com/watch?v=abcdefghijk", "id": "abcdefghijk", "title": "v", "videoId": "abcdefghijk"}]


@pytest.mark.parametrize("stdout", shapes(CHANNEL))
def test_youtube_channel_accepts_envelope(stdout):
    assert isinstance(parse_opencli_youtube_channel_json(stdout, collection_url="https://www.youtube.com/@x"), list)


PLAYLIST = [{"title": "v", "url": "https://www.youtube.com/watch?v=abcdefghijk", "video_id": "abcdefghijk"}]


def test_youtube_playlist_same_result_for_both_shapes():
    bare, wrapped = shapes(PLAYLIST)
    a = parse_opencli_youtube_playlist_output(bare, collection_url="https://www.youtube.com/playlist?list=PLx")
    b = parse_opencli_youtube_playlist_output(wrapped, collection_url="https://www.youtube.com/playlist?list=PLx")
    assert a and [i.id for i in a] == [i.id for i in b]


DOUYIN = [{"aweme_id": "7000000000000000001", "desc": "t", "url": "https://www.douyin.com/video/7000000000000000001"}]


def test_douyin_same_result_for_both_shapes():
    bare, wrapped = shapes(DOUYIN)
    expected = [i.id for i in parse_opencli_douyin_user_videos(bare)]
    assert expected == ["7000000000000000001"]
    assert [i.id for i in parse_opencli_douyin_user_videos(wrapped)] == expected


def test_xiaoyuzhou_same_result_for_both_shapes():
    rows = [{"eid": "e1", "title": "t"}]
    bare, wrapped = shapes(rows)
    assert [i.id for i in parse_xiaoyuzhou_episode_items(bare, podcast_id="p")] == ["e1"]
    assert [i.id for i in parse_xiaoyuzhou_episode_items(wrapped, podcast_id="p")] == ["e1"]


def test_bilibili_subtitles_and_default_json_user_videos():
    subtitle = [{"index": 1, "from": "0:00", "to": "0:01", "content": "hello"}]
    bare, wrapped = shapes(subtitle)
    assert len(parse_bilibili_subtitle_segments(bare)) == len(parse_bilibili_subtitle_segments(wrapped)) == 1
    videos = [{"title": "t", "url": "https://www.bilibili.com/video/BV1xx411c7mD"}]
    for stdout in shapes(videos):
        assert parse_opencli_bilibili_user_videos(stdout)


def test_xiaohongshu_note_same_result_for_both_shapes():
    rows = [{"title": "t", "content": "body text"}]
    bare, wrapped = shapes(rows)
    expected = parse_opencli_xiaohongshu_note_output(bare).text
    assert "body text" in expected
    assert parse_opencli_xiaohongshu_note_output(wrapped).text == expected


def test_xiaoe_content_same_result_for_both_shapes():
    rows = [{"title": "t", "content": "body text", "content_text": "body text"}]
    bare, wrapped = shapes(rows)
    expected = parse_xiaoe_content_result(bare)
    assert expected is not None
    assert parse_xiaoe_content_result(wrapped) == expected


def test_runtime_provider_requires_machine_json_instead_of_publishing_envelope():
    from openmd.backends.xiaoe import build_opencli_xiaoe_play_url_args

    args = build_opencli_xiaoe_play_url_args("https://example.invalid/video")
    assert args[args.index("-f") + 1] == "json"
    assert args.count("-f") == 1
    assert load_opencli_json('{"ok":true,"data":[{"content":"source text"}]}') == [{"content":"source text"}]


def test_empty_manifest_status_requires_complete_discovery():

    from openmd.core import receipt

    counts = dict.fromkeys(("verified", "partial", "terminal_gap", "retryable_failure", "blocked", "failed", "unattempted", "discovered"), 0)
    status = receipt.CompactReceiptRun._status_for
    assert status(counts, discovery_complete=True, manifest_only=True) == "discovery_only"
    assert status(counts, discovery_complete=False, manifest_only=True) == "blocked"
    assert status(counts, discovery_complete=True, manifest_only=False) == "blocked"
