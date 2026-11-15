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
    monkeypatch.setattr(post_video.shutil, "which", lambda name: "C:/bin/yt-dlp.exe")


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


def _asr(path):
    return TranscriptResult(text="[00:00:00-00:00:02] Hello there.\n[00:00:03-00:00:05] Second line.", status="verified", source_level="asr", backend="asr.auto")


def test_only_x_status_urls_apply():
    assert post_video.applies_to(TWEET)
    assert post_video.applies_to("https://twitter.com/a/status/1")
    assert not post_video.applies_to("https://x.com/someone")
    assert not post_video.applies_to("https://www.youtube.com/watch?v=abc")


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
    assert not post_video.append_video_section(md, TWEET, "note", run=_fake_run(durations=()))
    assert md.read_text(encoding="utf-8") == before


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
    assert "![视频](<assets/video-01.mp4>)" in text and "（第 2 段）" in text
    assert (tmp_path / "assets" / "video-02.mp4").is_file()


def test_download_failure_is_a_note_not_an_error(tmp_path):
    md = _post(tmp_path)
    post_video.append_video_section(md, TWEET, "text", run=_fake_run(download_ok=False), transcribe=_asr)
    assert "转文字未完成：视频下载失败" in md.read_text(encoding="utf-8")


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
