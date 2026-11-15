"""Collection discovery and resume use the same transcript pipeline."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmd import cli
from openmd.backends.base import BackendExtractRequest, DiscoveryRequest, DiscoveryResult
from openmd.backends.douyin_detail import DouyinDetailProvider
from openmd.backends.douyin import OpenCliDouyinUserVideosProvider, parse_opencli_douyin_user_videos
from openmd.backends.douyin_providers import DouyinTranscriptProvider
from openmd.backends.executor import BackendExecutor, DiscoveryExecutor, DiscoveryProviderRegistry
from openmd.core.manifest import discovery_manifest_row
from openmd.core.models import TranscriptResult
from test_douyin_local_transcript import LocalAsr, OPTIONS, VIDEO_ID, detail, item, runner


AUTHOR = "https://www.douyin.com/user/MS4wLjABAAAAfixture"


def collection_items():
    return [replace(item(play_url="https://media.example/" + str(index)), id=str(int(VIDEO_ID) + index), url="https://www.douyin.com/video/" + str(int(VIDEO_ID) + index), input_type="collection", title="Video " + str(index)) for index in range(3)]


def test_discovery_keeps_media_transient_and_manifest_excludes_it():
    parsed = parse_opencli_douyin_user_videos(json.dumps({"ok": True, "data": [{"aweme_id": VIDEO_ID, "play_url": "https://media.example/video", "duration_s": 68, "transcript_candidates": [{"text": "原始字幕"}]}]}))
    assert len(parsed) == 1
    assert parsed[0].raw_meta["play_url"] == "https://media.example/video"
    assert parsed[0].raw_meta["duration"] == "68"
    manifest = discovery_manifest_row(parsed[0], collection_url=AUTHOR, provider="fixture")
    assert "play_url" not in manifest and "transcript_candidates" not in manifest


def test_author_share_resolves_bounded_owned_session_then_user_videos():
    calls = []
    def command(args, *, timeout):
        calls.append(args)
        if args[1:3] == ["browser", "eval"]:
            return 0, json.dumps({"ok": True, "data": {"url": AUTHOR}}), ""
        if args[1:3] == ["douyin", "user-videos"]:
            return 0, json.dumps([{"aweme_id": VIDEO_ID}]), ""
        return 0, "{}", ""
    extracted = OpenCliDouyinUserVideosProvider(runner=command).discover(DiscoveryRequest("https://v.douyin.com/author/", "douyin", limit=3))
    assert extracted.ok
    assert extracted.metadata["normalized_url"] == AUTHOR
    assert [args[1:3] for args in calls] == [["browser", "open"], ["browser", "eval"], ["browser", "close"], ["douyin", "user-videos"]]
    assert "--all" not in str(calls)
    assert calls[-1][calls[-1].index("--limit") + 1] == "3"


def test_expired_discovered_media_refreshes_detail_once_and_uses_local_asr():
    detail_calls = []
    media_calls = []
    def media(command, timeout):
        media_calls.append(command)
        if len(media_calls) == 1:
            return 1, "", "expired"
        return runner(command, timeout)
    local = LocalAsr()
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(calls=detail_calls), runner=media, asr_backend=local)
    extracted = provider.extract(BackendExtractRequest(item(play_url="https://media.example/stale"), (provider.name,)))
    assert extracted.has_usable_text
    assert len(detail_calls) == 1 and len(media_calls) == 2 and len(local.paths) == 1
    assert all(not path.exists() for path in local.paths)


def test_two_failed_media_attempts_are_terminal_and_cleaned():
    calls = []
    paths = []
    def media(command, timeout):
        paths.append(Path(command[-1]))
        return 1, "", "expired"
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail(calls=calls), runner=media, asr_backend=LocalAsr())
    extracted = provider.extract(BackendExtractRequest(item(play_url="https://media.example/stale"), (provider.name,)))
    assert extracted.error_code == "douyin_media_url_unavailable"
    assert len(calls) == 1 and len(paths) == 2
    assert all(not path.parent.exists() for path in paths)


def install_cli_fakes(monkeypatch, provider, *, items=None):
    class Discovery:
        name = "opencli.douyin.user-videos"
        def discover(self, request):
            assert request.limit == 3 and request.allow_all is False
            return DiscoveryResult.success(provider=self.name, items=items if items is not None else collection_items())
    discovery = Discovery()
    registry = DiscoveryProviderRegistry({discovery.name: discovery}, platform_defaults={"douyin": discovery.name})
    monkeypatch.setattr(cli, "_build_discovery_executor", lambda *a, **k: DiscoveryExecutor(provider_registry=registry))
    monkeypatch.setattr(cli, "_build_backend_executor", lambda *a, **k: BackendExecutor(providers={"douyin.transcript": provider}))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)


def command(out, *extra):
    return ["collect", AUTHOR, "--limit", "3", "--out", str(out), "--enable-browser-backends", *extra]


def test_collection_enriches_missing_metadata_without_replacing_media_or_transcript():
    detail_calls = []
    media_calls = []
    def media(args, timeout):
        media_calls.append(args)
        return runner(args, timeout)
    provider = DouyinTranscriptProvider(
        OPTIONS,
        detail_provider=detail({"aweme_id": VIDEO_ID, "desc": "详情另一个标题", "author": {"nickname": "土豆求职"}, "created": "2026-11-09 14:43:21", "play_url": "https://media.example/detail"}, detail_calls),
        runner=media, asr_backend=LocalAsr(),
    )
    original = replace(item(play_url="https://media.example/discovered"), input_type="collection", title="发现的原标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.title == original.title
    assert result.author == "土豆求职" and result.published_at == "2026-11-09 14:43:21"
    assert len(detail_calls) == len(media_calls) == 1
    assert media_calls[0][media_calls[0].index("-i") + 1] == "https://media.example/discovered"


def test_collection_enrichment_preserves_native_caption_fast_path():
    calls = []
    provider = DouyinTranscriptProvider(
        OPTIONS,
        detail_provider=detail({"aweme_id": VIDEO_ID, "author": "作者", "created": "2026-11-09", "subtitles": [{"text": "详情其他字幕"}]}, calls),
        runner=lambda *_: pytest.fail("native captions must not download media"),
    )
    original = replace(item(subtitles=[{"text": "原始字幕"}]), input_type="collection", title="标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.text == "原始字幕\n"
    assert result.author == "作者" and result.published_at == "2026-11-09"
    assert len(calls) == 1


def test_collection_reuses_metadata_detail_if_discovered_media_expired():
    calls, media_calls = [], []
    def media(args, timeout):
        media_calls.append(args)
        if len(media_calls) == 1:
            return 1, "", "expired"
        return runner(args, timeout)
    local = LocalAsr()
    provider = DouyinTranscriptProvider(
        OPTIONS,
        detail_provider=detail({"aweme_id": VIDEO_ID, "author": "作者", "created": "2026-11-09", "play_url": "https://media.example/fresh"}, calls),
        runner=media, asr_backend=local,
    )
    original = replace(item(play_url="https://media.example/stale"), input_type="collection", title="原标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.has_usable_text and result.author == "作者"
    assert len(calls) == len(local.paths) == 1 and len(media_calls) == 2
    assert [args[args.index("-i") + 1] for args in media_calls] == ["https://media.example/stale", "https://media.example/fresh"]


def test_collection_image_metadata_enrichment_preserves_author_body():
    calls = []
    body = "[00:01] 作者原文\n\n重复\n重复  \n"
    provider = DouyinTranscriptProvider(
        OPTIONS,
        detail_provider=detail({"aweme_id": VIDEO_ID, "author": "作者", "created": "2026-11-09", "images": ["image"], "desc": "详情其他文案"}, calls),
        runner=lambda *_: pytest.fail("image author text must not download media"),
    )
    original = replace(item(), input_type="collection", source_type="image_text", title=body)
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.text == body and result.author == "作者" and result.published_at == "2026-11-09"
    assert result.source_level == "native_text" and len(calls) == 1


def test_complete_collection_metadata_avoids_extra_detail():
    provider = DouyinTranscriptProvider(
        OPTIONS, detail_provider=DouyinDetailProvider(runner=lambda *a, **k: pytest.fail("complete metadata must not read detail")),
        runner=runner, asr_backend=LocalAsr(),
    )
    original = replace(item(play_url="https://media.example/discovered"), input_type="collection", title="标题", author="发现作者", published_at="2026-11-09")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.author == original.author and result.published_at == original.published_at


def test_metadata_detail_failure_keeps_available_native_content():
    calls = []
    def failed_detail(*args, **kwargs):
        calls.append(args)
        return 1, "", "unavailable"
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=DouyinDetailProvider(runner=failed_detail))
    original = replace(item(subtitles=[{"text": "已有字幕"}]), input_type="collection", title="标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.text == "已有字幕\n" and result.has_usable_text
    assert len(calls) == 2
    assert any("Douyin metadata incomplete" in note for note in result.source_notes)
    assert result.metadata["metadata_enrichment"] == {"detail_attempts": 2, "missing_fields": ["author", "published_at"], "detail_error_codes": ["douyin_detail_read_failed", "douyin_detail_read_failed"]}


def test_metadata_transient_failure_retries_before_one_media_download():
    calls, media_calls = [], []
    def read_detail(args, *, timeout):
        calls.append(args)
        if len(calls) == 1:
            return 1, "", "temporary failure with sensitive diagnostics"
        return 0, json.dumps({"aweme_id": VIDEO_ID, "author": "土豆求职", "created": "2026-11-08 15:14:05", "desc": "不是逐字稿", "play_url": "https://media.example/detail"}), ""
    def media(args, timeout):
        media_calls.append(args)
        return runner(args, timeout)
    local = LocalAsr()
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=DouyinDetailProvider(runner=read_detail), runner=media, asr_backend=local)
    original = replace(item(play_url="https://media.example/discovered"), input_type="collection", title="原标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.author == "土豆求职" and result.published_at == "2026-11-08 15:14:05"
    assert len(calls) == 2 and len(media_calls) == len(local.paths) == 1
    assert media_calls[0][media_calls[0].index("-i") + 1] == "https://media.example/discovered"
    assert result.text == local.value.text and "不是逐字稿" not in result.text
    assert "metadata_warnings" not in result.metadata


def test_successful_detail_without_required_fields_is_retried_and_diagnosed():
    calls = []
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=detail({"aweme_id": VIDEO_ID, "desc": "作品文案不是正文"}, calls))
    original = replace(item(subtitles=[{"text": "真正的原生字幕"}]), input_type="collection", title="标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.text == "真正的原生字幕\n" and len(calls) == 2
    assert result.metadata["metadata_enrichment"] == {"detail_attempts": 2, "missing_fields": ["author", "published_at"], "detail_error_codes": []}
    assert any("missing author, published_at" in note for note in result.source_notes)


def test_permanent_metadata_failure_preserves_asr_and_package_warning(tmp_path):
    from openmd.core.package_writer import build_package_metadata
    calls, media_calls = [], []
    def read_detail(args, *, timeout):
        calls.append(args)
        return 1, "private response", "private diagnostics"
    def media(args, timeout):
        media_calls.append(args)
        return runner(args, timeout)
    local = LocalAsr()
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=DouyinDetailProvider(runner=read_detail), runner=media, asr_backend=local)
    original = replace(item(play_url="https://media.example/discovered"), input_type="collection", title="原作品标题")
    result = provider.extract(BackendExtractRequest(original, (provider.name,)))
    assert result.text == local.value.text and result.has_usable_text
    assert len(calls) == 2 and len(media_calls) == len(local.paths) == 1
    metadata = build_package_metadata(original, result, created_at="2026-11-10T00:00:00Z", updated_at="2026-11-10T00:00:00Z")
    assert any("Douyin metadata incomplete" in warning for warning in metadata["warnings"])
    assert "private" not in str(result) and "private" not in str(metadata)


@pytest.mark.parametrize("package", [False, True])
@pytest.mark.parametrize("discovered_metadata", [False, True])
def test_collect_metadata_survives_plain_and_package_outputs(monkeypatch, tmp_path, capsys, package, discovered_metadata):
    rows = []
    for index in range(3):
        row = {"aweme_id": str(int(VIDEO_ID) + index), "title": f"作品 {index}", "play_url": f"https://media.example/discovered-{index}"}
        if discovered_metadata:
            row.update({"author": {"nickname": "发现作者"}, "created": f"2026-11-0{index + 7} 15:14:05"})
        rows.append(row)
    discovered = parse_opencli_douyin_user_videos(json.dumps(rows))
    calls, media_calls = [], []
    def read_detail(args, *, timeout):
        calls.append(args)
        work_id = args[3]
        index = int(work_id) - int(VIDEO_ID)
        return 0, json.dumps({"aweme_id": work_id, "author": {"nickname": "详情作者"}, "created": f"2026-11-0{index + 7} 15:14:05", "desc": "不得覆盖原标题", "play_url": "https://media.example/detail"}), ""
    def media(args, timeout):
        media_calls.append(args)
        return runner(args, timeout)
    provider = DouyinTranscriptProvider(OPTIONS, detail_provider=DouyinDetailProvider(runner=read_detail), runner=media, asr_backend=LocalAsr())
    install_cli_fakes(monkeypatch, provider, items=discovered)
    assert cli.main(command(tmp_path, *(["--package"] if package else []))) == 0
    assert len(calls) == (0 if discovered_metadata else 3)
    assert len(media_calls) == 3
    for index, args in enumerate(media_calls):
        assert args[args.index("-i") + 1] == f"https://media.example/discovered-{index}"
    author = "发现作者" if discovered_metadata else "详情作者"
    if package:
        metadata_files = list(tmp_path.rglob("metadata.json"))
        assert len(metadata_files) == 3
        for path in metadata_files:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            index = int(metadata["title"].split()[-1])
            published = f"2026-11-0{index + 7} 15:14:05"
            assert metadata["author"] == author and metadata["published_at"] == published
            output = (path.parent / "index.md").read_text(encoding="utf-8")
            assert f"- Author: {author}" in output and f"- Published at: {published}" in output
    else:
        outputs = list((tmp_path / "douyin").glob("*.md"))
        assert len(outputs) == 3
        for path in outputs:
            output = path.read_text(encoding="utf-8")
            assert f'author: "{author}"' in output
            index = int(path.stem.split()[-1])
            assert f'published: "2026-11-0{index + 7}"' in output
            assert "这是开头" in output and "这是结尾" in output


def test_manifest_only_does_no_extraction(monkeypatch, tmp_path, capsys):
    class NeverExtract:
        def extract(self, request):
            pytest.fail("manifest must not transcribe")
    install_cli_fakes(monkeypatch, NeverExtract())
    assert cli.main(command(tmp_path, "--manifest-only")) == 0
    manifest = next((tmp_path / "manifests").glob("*.jsonl"))
    assert len(manifest.read_text(encoding="utf-8").splitlines()) == 3
    assert "media.example" not in manifest.read_text(encoding="utf-8")
    assert not list(tmp_path.rglob("*.md"))


def test_collection_item_failure_is_visible_and_does_not_stop_remaining_items(monkeypatch, tmp_path, capsys):
    class Provider:
        calls = []
        def extract(self, request):
            self.calls.append(request.item.id)
            if len(self.calls) == 2:
                return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="douyin.transcript", error_code="douyin_no_speech")
            return TranscriptResult(text="[00:00:01] 原始逐字稿", status="verified", source_level="media_asr", backend="asr.qwen3")
    provider = Provider()
    install_cli_fakes(monkeypatch, provider)
    assert cli.main(command(tmp_path)) == 1
    assert len(provider.calls) == 3
    output = json.loads(capsys.readouterr().out)
    assert output["success_count"] == 2 and output["failed_count"] == 1
    assert output["failure_code_counts"]["douyin_no_speech"] == 1
    assert "douyin_no_speech" in (tmp_path / "runlog.jsonl").read_text(encoding="utf-8")


def test_package_resume_after_interruption_does_not_retranscribe_completed_item(monkeypatch, tmp_path, capsys):
    class Interrupted:
        calls = []
        interrupt = True
        def extract(self, request):
            if self.interrupt and len(self.calls) == 1:
                raise KeyboardInterrupt()
            self.calls.append(request.item.id)
            return TranscriptResult(text="[00:00:01] 原始逐字稿", status="verified", source_level="media_asr", backend="asr.qwen3")
    provider = Interrupted()
    install_cli_fakes(monkeypatch, provider)
    args = command(tmp_path, "--package", "--skip-existing")
    assert cli.main(args) == 130
    assert len(provider.calls) == 1
    provider.interrupt = False
    assert cli.main(args) == 0
    assert len(provider.calls) == 3 and len(set(provider.calls)) == 3
    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[-1])["skipped_count"] == 1


def test_batch_file_multiple_video_inputs_use_same_transcript_backend(monkeypatch, tmp_path, capsys):
    class Provider:
        calls = []
        def extract(self, request):
            self.calls.append(request.item.id)
            return TranscriptResult(text="[00:00:01] 原文", status="verified", source_level="platform_transcript", backend="douyin.native-transcript")
    provider = Provider()
    install_cli_fakes(monkeypatch, provider)
    inputs = tmp_path / "urls.txt"
    inputs.write_text("\n".join(entry.url for entry in collection_items()), encoding="utf-8")
    assert cli.main(["batch", str(inputs), "--out", str(tmp_path / "out")]) == 0
    assert len(provider.calls) == 3


def test_rate_limit_is_shared_across_douyin_extractions(monkeypatch):
    import argparse
    waits = []
    clock = iter([0.0, 0.2, 1.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli.time, "sleep", waits.append)
    args = argparse.Namespace(backend=None)
    cli._pace_douyin_extraction(item(), args)
    cli._pace_douyin_extraction(item(), args)
    assert waits == [pytest.approx(0.8)]
