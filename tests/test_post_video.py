from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from openmd import browser_host, post_video
from openmd.core.models import TranscriptResult
from openmd.core.readable import format_transcript
from openmd.interactive import _attach_video

TWEET = "https://x.com/someone/status/2115815669581455750"
FOOTER = "*来源：第三方提取；仅此条帖子，未验证整串讨论覆盖。*"


def test_real_runner_preserves_utf8_text_capture():
    result = post_video._run([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'out\\xff');sys.stderr.buffer.write(b'err\\xff')"], 5)
    assert result.returncode == 0
    assert result.stdout == "out\ufffd" and result.stderr == "err\ufffd"


@pytest.fixture(autouse=True)
def yt_dlp_present(monkeypatch):
    monkeypatch.setattr(post_video, "yt_dlp_command", lambda: ["C:/bin/yt-dlp.exe"])


def _post(tmp_path: Path) -> Path:
    path = tmp_path / "post.md"
    path.write_text(f"---\ntitle: t\n---\n\n# t\n\nbody text\n\n{FOOTER}\n", encoding="utf-8")
    return path


def _fake_run(durations=("74.8",), download_ok=True, calls=None):
    def run(command, timeout):
        if calls is not None:
            calls.append(command)
        if "--simulate" in command:
            return subprocess.CompletedProcess(command, 0, "\n".join(durations) + "\n", "")
        if not download_ok:
            return subprocess.CompletedProcess(command, 1, "", "HTTP Error 403")
        out = Path(command[command.index("-o") + 1]).parent
        for index, _ in enumerate(durations, 1):
            (out / f"video-{index:02d}.mp4").write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, "", "")
    return run


def test_text_mode_keeps_managed_module_prefix_intact(tmp_path, monkeypatch):
    prefix = [sys.executable, "-m", "yt_dlp"]
    monkeypatch.setattr(post_video, "yt_dlp_command", lambda: list(prefix))
    calls = []
    md = _post(tmp_path)
    post_video.append_video_section(md, TWEET, "text", run=_fake_run(calls=calls), transcribe=_asr)
    download = next(command for command in calls if "--simulate" not in command)
    assert download[:3] == prefix
    assert download.index("-S") > 2 and download[download.index("-S") + 1] == "res:480"


def _asr(path):
    return TranscriptResult(text="[00:00:00-00:00:02] Hello there.\n[00:00:03-00:00:05] Second line.", status="verified", source_level="asr", backend="asr.auto")


def test_all_public_pages_apply_but_native_transcript_routes_skip_text():
    assert post_video.applies_to(TWEET)
    assert post_video.applies_to("https://twitter.com/a/status/1")
    assert post_video.applies_to("https://x.com/someone")
    assert post_video.applies_to("https://www.reddit.com/r/test/comments/abc")
    assert not post_video.should_process("https://www.youtube.com/watch?v=abc", "text")
    assert post_video.should_process("https://www.youtube.com/watch?v=abc", "file")
    assert post_video.should_process("https://www.bilibili.com/video/BV1abc123", "file")
    assert post_video.native_media_source("https://www.bilibili.com/video/BV1abc123") == "bilibili"
    assert post_video.native_media_source("https://www.youtube.com/watch?v=abc") is None


def test_note_mode_only_applies_to_x_status_pages():
    assert post_video.should_process("https://x.com/someone/status/123", "note")
    assert not post_video.should_process("https://example.org/article", "note")
    assert not post_video.should_process("https://www.youtube.com/watch?v=abc", "note")


def test_default_note_mode_leaves_non_x_markdown_untouched(tmp_path):
    calls = []
    md = _post(tmp_path)
    before = md.read_text(encoding="utf-8")
    assert not post_video.append_video_section(md, "https://example.org/article", "note", run=_fake_run(calls=calls))
    assert calls == []
    assert md.read_text(encoding="utf-8") == before


def test_native_transcript_file_download_does_not_transcribe(tmp_path):
    calls = []
    md = _post(tmp_path)
    section = post_video.video_section(
        "https://www.youtube.com/watch?v=abc",
        "file",
        tmp_path,
        run=_fake_run(calls=calls),
        transcribe=lambda _path: pytest.fail("native route transcript must not be repeated"),
        transcribe_media=False,
    )
    assert section and "assets/video-01.mp4" in section
    assert len(calls) == 2 and "--simulate" in calls[0]


def test_retained_native_media_skips_download_and_transcribe(tmp_path, monkeypatch):
    md = _post(tmp_path)
    stage = tmp_path / "stage"
    raw = stage / "raw-media"
    raw.mkdir(parents=True)
    (raw / "douyin-audio.mp4").write_bytes(b"mp4")
    args = argparse.Namespace(video="file", enable_network_backends=True, enable_media_download=True, enable_asr=True, json=True)
    monkeypatch.setattr(post_video, "append_video_section", lambda *a, **kw: pytest.fail("should reuse retained media"))
    _attach_video(args, "https://www.douyin.com/video/123", md, stage=stage)
    assert (tmp_path / "assets" / "douyin-audio.mp4").is_file()


def test_bilibili_file_mode_publishes_native_raw_media(tmp_path, monkeypatch):
    md = _post(tmp_path)
    stage = tmp_path / "stage"
    raw = stage / "raw-media"
    raw.mkdir(parents=True)
    (raw / "bilibili-video.mp4").write_bytes(b"mp4")
    args = argparse.Namespace(video="file", enable_network_backends=True, enable_media_download=True, enable_asr=True, json=True)
    monkeypatch.setattr(post_video, "append_video_section", lambda *a, **kw: pytest.fail("should reuse retained media"))
    _attach_video(args, "https://www.bilibili.com/video/BV1abc123", md, stage=stage)
    assert (tmp_path / "assets" / "bilibili-video.mp4").is_file()


def test_native_transcript_text_skips_post_video_download_and_asr(tmp_path, monkeypatch):
    md = _post(tmp_path)
    args = argparse.Namespace(video="text", enable_network_backends=True, enable_media_download=True, enable_asr=True, json=True)
    monkeypatch.setattr(post_video, "append_video_section", lambda *a, **kw: pytest.fail("native transcript route must be skipped"))
    _attach_video(args, "https://www.youtube.com/watch?v=abc", md)
    assert md.read_text(encoding="utf-8").count("## 视频逐字稿") == 0


def test_note_mode_only_probes_and_keeps_footer_last(tmp_path):
    calls = []
    md = _post(tmp_path)
    assert post_video.append_video_section(md, TWEET, "note", run=_fake_run(calls=calls))
    text = md.read_text(encoding="utf-8")
    assert "本帖含视频（约 1 分 15 秒），未转文字" in text
    assert text.rstrip().endswith(FOOTER)
    assert len(calls) == 1 and "--simulate" in calls[0]


def test_post_without_video_is_left_untouched(tmp_path):
    md = _post(tmp_path)
    before = md.read_text(encoding="utf-8")
    assert post_video.append_video_section(md, TWEET, "text", run=_fake_run(durations=()))
    assert md.read_text(encoding="utf-8") != before
    assert "未发现可下载的视频" in md.read_text(encoding="utf-8")


def test_probe_block_is_kept_as_next_action_note(tmp_path):
    md = _post(tmp_path)
    def blocked(command, timeout):
        return subprocess.CompletedProcess(command, 1, "", "HTTP 403 forbidden")
    assert post_video.append_video_section(md, "https://www.reddit.com/r/test/comments/abc", "text", run=blocked)
    text = md.read_text(encoding="utf-8")
    assert "下一步" in text and "yt-dlp" in text


def test_text_mode_appends_readable_transcript_and_removes_media(tmp_path, monkeypatch):
    calls = []
    md = _post(tmp_path)
    post_video.append_video_section(md, TWEET, "text", run=_fake_run(calls=calls), transcribe=_asr)
    text = md.read_text(encoding="utf-8")
    assert "## 视频逐字稿" in text and "**00:00:00**" in text and "Hello there. Second line." in text
    assert text.rstrip().endswith(FOOTER)
    download = calls[1]
    assert "-x" not in download and "res:480" in download and "--socket-timeout" in download  # progressive mp4, no HLS audio
    assert not (tmp_path / "assets").exists()
    assert not Path(download[download.index("-o") + 1]).parent.exists()


def test_file_mode_keeps_video_next_to_markdown(tmp_path):
    md = _post(tmp_path)
    post_video.append_video_section(md, TWEET, "file", run=_fake_run(durations=("10", "20")), transcribe=_asr)
    text = md.read_text(encoding="utf-8")
    assert "本帖含 2 段视频（共约 30 秒）" in text
    assert "![视频/音频](<assets/video-01.mp4>)" in text and "大小：" in text and "（第 2 段）" in text
    assert (tmp_path / "assets" / "video-02.mp4").is_file()


def test_native_route_media_is_published_from_retained_staging(tmp_path):
    md = _post(tmp_path)
    raw = tmp_path / "stage" / "raw-media"
    raw.mkdir(parents=True)
    (raw / "native-audio.wav").write_bytes(b"wav")
    assert post_video.append_retained_media_section(md, raw)
    text = md.read_text(encoding="utf-8")
    assert "本站点下载路线保留" in text
    assert "![视频/音频](<assets/native-audio.wav>)" in text
    assert "大小：3 B" in text
    assert (tmp_path / "assets" / "native-audio.wav").is_file()
    assert not list(raw.iterdir())


def test_xiaoe_native_file_request_never_falls_back_to_generic_media_download(tmp_path, monkeypatch):
    md = _post(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    args = argparse.Namespace(video="file", enable_network_backends=True, enable_media_download=True, enable_asr=True, json=True)
    monkeypatch.setattr(post_video, "append_video_section", lambda *a, **kw: pytest.fail("generic download must not run for Xiaoe"))
    _attach_video(args, "https://demo.xiaoe-tech.com/course/lesson", md, stage=stage)
    assert "受加密或访问控制的流仅用于转写" in md.read_text(encoding="utf-8")


def test_download_failure_is_a_note_not_an_error(tmp_path):
    md = _post(tmp_path)
    post_video.append_video_section(md, TWEET, "text", run=_fake_run(download_ok=False), transcribe=_asr)
    text = md.read_text(encoding="utf-8")
    assert "转文字未完成：视频下载失败" in text
    assert 'python -m pip install -U "yt-dlp[default]"' in text and "`openmd doctor` 可查看当前版本" in text


def test_reddit_login_failure_reports_manual_next_step(tmp_path):
    def blocked(command, timeout):
        if "--simulate" in command:
            return subprocess.CompletedProcess(command, 0, "20\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Account authentication is required")

    section = post_video.video_section(
        "https://www.reddit.com/r/test/comments/abc", "file", tmp_path,
        run=blocked, transcribe_media=False,
    )

    assert "login_required" in section
    assert "该站点要求登录才能下载视频" in section
    assert "浏览器中手动下载" in section
    assert "不读取浏览器 cookie" in section


def test_probe_error_uses_sanitized_category_and_action(tmp_path):
    def blocked(command, timeout):
        return subprocess.CompletedProcess(command, 1, "", "HTTP Error 403: Forbidden")

    section = post_video.video_section(TWEET, "text", tmp_path, run=blocked, transcribe_media=False)

    assert "forbidden_or_outdated" in section
    assert "openmd doctor" in section
    assert "HTTP Error 403" not in section


def test_text_mode_without_media_gates_falls_back_to_note(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(post_video, "append_video_section", lambda md, url, mode, **kw: seen.append(mode))
    args = argparse.Namespace(video="text", enable_network_backends=True, enable_media_download=False, enable_asr=False, json=True)
    _attach_video(args, TWEET, _post(tmp_path))
    assert seen == ["note"]
    args.enable_network_backends = False
    _attach_video(args, TWEET, _post(tmp_path))
    assert seen == ["note"]  # no network grant: nothing is probed at all


def test_asr_range_timestamps_merge_into_paragraphs():
    out = format_transcript("[00:00:00-00:00:02] One.\n[00:00:02-00:00:04] Two.", source_type="video", platform="x")
    assert out == "**00:00:00**\n\nOne. Two."


def test_host_passes_video_mode_to_openmd(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(browser_host, "_spawn_runner", lambda *a, **k: None)
    status = browser_host.start_job(TWEET, str(tmp_path / "dest"), video="file", spawn=lambda *a, **k: None)
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "saved_to": str(tmp_path / "dest" / "x.md")}), "")

    browser_host.run_job(browser_host.jobs_root() / status["id"], runner=run)
    assert seen[0][seen[0].index("--video") + 1] == "file"
    reply = browser_host.handle({"type": "save", "url": TWEET, "save_to": str(tmp_path), "video": "everything"})
    assert not reply["ok"] and "invalid_video_mode" in reply["error"]
    batch = browser_host.handle({"type": "save_many", "urls": [TWEET], "save_to": str(tmp_path)})
    assert browser_host.read_status(batch["jobs"][0]["id"])["video"] == "note"
