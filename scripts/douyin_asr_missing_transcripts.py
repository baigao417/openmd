"""Fill missing/placeholder Douyin transcripts via transient audio + ASR.

This script is intentionally an operator-run helper, not a safe default backend:

- reads existing Markdown under a target directory;
- only targets likely placeholder/low-content transcript sections;
- obtains at most the current OpenCLI-supported 20 recent playback URLs in memory;
- extracts temporary audio with ffmpeg;
- sends the temporary audio to SiliconFlow ASR;
- writes back only successful transcript text;
- does not persist videos, play URLs, raw API responses, cookies, tokens, or headers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


NOISE_RE = re.compile(r"^(字幕by|MING PAO CANADA|Always in my mind|好$|\s*$)", re.I)
TIMESTAMP_PREFIX_RE = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")
DOUYIN_VIDEO_RE = re.compile(r"https://www\.douyin\.com/video/(\d+)")
SPACE_RE = re.compile(r"\s+")
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"
MAX_OPENCLI_DOUYIN_LIMIT = 20


@dataclass(frozen=True)
class Candidate:
    aweme_id: str
    path: Path
    rel_path: str
    title: str
    lines: int
    meaningful: int
    meaningful_chars: int


@dataclass(frozen=True)
class VideoRow:
    aweme_id: str
    title: str
    duration: int
    play_url: str


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def sanitize_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"(token|cookie|authorization|signature|x-signature)=([^&\s]+)", r"\1=[redacted]", text, flags=re.I)
    return text[:700]


def command_path(name: str) -> str:
    resolved = shutil.which(name) or shutil.which(f"{name}.cmd")
    if not resolved:
        raise RuntimeError(f"Required command not found on PATH: {name}")
    return resolved


def read_index_titles(root: Path) -> dict[str, str]:
    index_path = root / "index.json"
    if not index_path.is_file():
        return {}
    data = json.loads(index_path.read_text(encoding="utf-8", errors="replace"))
    items = data.get("items", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    titles: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("awemeId") or item.get("aweme_id") or "").strip()
        if aweme_id:
            titles[aweme_id] = str(item.get("title") or aweme_id).strip() or aweme_id
    return titles


def score_markdown(path: Path, root: Path, titles: dict[str, str]) -> Candidate | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = DOUYIN_VIDEO_RE.search(text)
    if not match:
        return None
    aweme_id = match.group(1)
    if "## 逐字稿" in text:
        body = text.split("## 逐字稿", 1)[1]
    elif "## Transcript" in text:
        body = text.split("## Transcript", 1)[1]
    else:
        body = ""
    lines: list[str] = []
    meaningful: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            break
        content = TIMESTAMP_PREFIX_RE.sub("", line).strip()
        if not content or content.startswith("<!--"):
            continue
        lines.append(content)
        if len(content) >= 4 and not NOISE_RE.search(content):
            meaningful.append(content)
    return Candidate(
        aweme_id=aweme_id,
        path=path,
        rel_path=str(path.relative_to(root)),
        title=titles.get(aweme_id, path.stem),
        lines=len(lines),
        meaningful=len(meaningful),
        meaningful_chars=sum(len(item) for item in meaningful),
    )


def discover_candidates(root: Path, *, min_meaningful: int, min_chars: int) -> list[Candidate]:
    titles = read_index_titles(root)
    candidates: list[Candidate] = []
    for path in sorted(root.rglob("*.md")):
        candidate = score_markdown(path, root, titles)
        if candidate is None:
            continue
        if candidate.meaningful < min_meaningful or candidate.meaningful_chars < min_chars:
            candidates.append(candidate)
    return candidates


def load_only_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    ids: set[str] = set()
    rows = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    for row in rows:
        if isinstance(row, str) and row.strip():
            ids.add(row.strip())
        elif isinstance(row, dict):
            aweme_id = str(row.get("awemeId") or row.get("aweme_id") or row.get("video_id") or "").strip()
            if aweme_id:
                ids.add(aweme_id)
    return ids


def bounded_opencli_limit(value: int | None) -> int:
    return max(1, min(int(value or MAX_OPENCLI_DOUYIN_LIMIT), MAX_OPENCLI_DOUYIN_LIMIT))


def build_opencli_user_videos_command(
    sec_uid: str,
    *,
    site_session: str,
    limit: int | None,
) -> list[str]:
    return [
        command_path("opencli"),
        "douyin",
        "user-videos",
        sec_uid,
        "--limit",
        str(bounded_opencli_limit(limit)),
        "--with_comments",
        "false",
        "--site-session",
        site_session,
        "-f",
        "json",
    ]


def run_opencli_user_videos(
    sec_uid: str,
    *,
    site_session: str,
    timeout: int,
    limit: int | None,
) -> list[VideoRow]:
    opencli = command_path("opencli")
    completed = subprocess.run(
        build_opencli_user_videos_command(sec_uid, site_session=site_session, limit=limit),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"opencli douyin user-videos failed: {sanitize_error(completed.stderr or completed.stdout)}")
    payload = json.loads(completed.stdout)
    raw_rows = payload if isinstance(payload, list) else payload.get("data") or payload.get("items") or []
    rows: list[VideoRow] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        aweme_id = str(row.get("aweme_id") or row.get("awemeId") or row.get("video_id") or "").strip()
        play_url = str(row.get("play_url") or "").strip()
        if not aweme_id or not play_url:
            continue
        try:
            duration = int(float(row.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        rows.append(
            VideoRow(
                aweme_id=aweme_id,
                title=str(row.get("title") or aweme_id).strip() or aweme_id,
                duration=max(0, duration),
                play_url=play_url,
            )
        )
    return rows


def extract_audio(play_url: str, output_path: Path, *, timeout: int) -> None:
    ffmpeg = command_path("ffmpeg")
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            "User-Agent: Mozilla/5.0\r\nReferer: https://www.douyin.com/\r\n",
            "-i",
            play_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {sanitize_error(completed.stderr or completed.stdout)}")


def clean_asr_text(text: str) -> str:
    text = re.sub(r"<\|[^|]+?\|>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text_lines(text: str) -> list[str]:
    text = clean_asr_text(text)
    if not text:
        return []
    if "\n" in text:
        parts = [part.strip() for part in text.splitlines() if part.strip()]
    else:
        parts = [
            part.strip()
            for part in re.split(r"(?<=[。！？!?])\s*", text)
            if part.strip()
        ]
    if len(parts) == 1 and len(parts[0]) > 120:
        value = parts[0]
        parts = [value[index : index + 80].strip() for index in range(0, len(value), 80)]
    return parts


def format_transcript(text: str) -> str:
    lines = split_text_lines(text)
    if not lines:
        return ""
    return "\n".join(f"[00:00:00] {line}" for line in lines).strip() + "\n"


def transcribe_siliconflow(audio_path: Path, *, timeout: int) -> tuple[str, dict[str, Any]]:
    api_key = os.getenv("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is not set")
    with audio_path.open("rb") as file_obj:
        response = requests.post(
            SILICONFLOW_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": SILICONFLOW_MODEL},
            files={"file": (audio_path.name, file_obj, "audio/mpeg")},
            timeout=timeout,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"SiliconFlow ASR HTTP {response.status_code}: {sanitize_error(response.text)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"SiliconFlow ASR returned non-JSON response: {sanitize_error(response.text)}") from exc
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text") or payload.get("transcription") or "").strip()
        if not text and isinstance(payload.get("data"), dict):
            text = str(payload["data"].get("text") or "").strip()
        if not text and isinstance(payload.get("segments"), list):
            text = "\n".join(str(seg.get("text") or "").strip() for seg in payload["segments"] if isinstance(seg, dict))
    if not text:
        raise RuntimeError(f"SiliconFlow ASR returned no text: {sanitize_error(payload)}")
    return text, payload if isinstance(payload, dict) else {}


def replace_transcript_section(original: str, transcript: str, *, run_id: str) -> str:
    marker = "## 逐字稿"
    note = (
        "\n<!-- openmd:asr-fill "
        f"provider=siliconflow model={SILICONFLOW_MODEL} run={run_id} "
        "media_retention=none -->\n"
    )
    new_section = f"{marker}\n\n{transcript.rstrip()}\n{note}"
    if marker not in original:
        return original.rstrip() + "\n\n" + new_section
    before, after = original.split(marker, 1)
    next_heading = re.search(r"\n## (?!逐字稿)", after)
    if next_heading:
        tail = after[next_heading.start() :]
        return before.rstrip() + "\n\n" + new_section + tail
    return before.rstrip() + "\n\n" + new_section + "\n"


def backup_file(path: Path, root: Path, backup_root: Path) -> Path:
    rel = path.relative_to(root)
    backup_path = backup_root / rel
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    return backup_path


def write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def process_candidate(
    candidate: Candidate,
    video: VideoRow,
    *,
    root: Path,
    backup_root: Path,
    runlog: Path,
    run_id: str,
    ffmpeg_timeout: int,
    asr_timeout: int,
    dry_run: bool,
) -> bool:
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="openmd-douyin-asr-") as tmpdir:
            audio_path = Path(tmpdir) / f"{candidate.aweme_id}.mp3"
            extract_audio(video.play_url, audio_path, timeout=ffmpeg_timeout)
            text, payload = transcribe_siliconflow(audio_path, timeout=asr_timeout)
            transcript = format_transcript(text)
            if len(transcript.strip()) < 20:
                raise RuntimeError("ASR transcript too short after formatting")
            if not dry_run:
                backup = backup_file(candidate.path, root, backup_root)
                original = candidate.path.read_text(encoding="utf-8", errors="replace")
                candidate.path.write_text(
                    replace_transcript_section(original, transcript, run_id=run_id),
                    encoding="utf-8",
                )
            else:
                backup = None
        write_jsonl(
            runlog,
            {
                "aweme_id": candidate.aweme_id,
                "status": "success",
                "path": str(candidate.path),
                "chars": len(transcript),
                "line_count": len([line for line in transcript.splitlines() if line.strip()]),
                "asr_backend": "siliconflow",
                "asr_model": SILICONFLOW_MODEL,
                "payload_keys": sorted(payload.keys())[:20],
                "backup": str(backup) if backup else None,
                "elapsed_seconds": round(time.time() - started, 2),
                "dry_run": dry_run,
            },
        )
        return True
    except Exception as exc:
        write_jsonl(
            runlog,
            {
                "aweme_id": candidate.aweme_id,
                "status": "failed",
                "path": str(candidate.path),
                "error": sanitize_error(exc),
                "elapsed_seconds": round(time.time() - started, 2),
                "dry_run": dry_run,
            },
        )
        return False


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    run_root = Path(args.run_root).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or now_stamp()
    runlog = run_root / f"asr-fill-{run_id}.jsonl"
    backup_root = run_root / f"backups-{run_id}"

    candidates = discover_candidates(root, min_meaningful=args.min_meaningful, min_chars=args.min_chars)
    only_ids = load_only_ids(Path(args.only_ids).expanduser().resolve() if args.only_ids else None)
    if only_ids is not None:
        candidates = [candidate for candidate in candidates if candidate.aweme_id in only_ids]
    if args.aweme_id:
        selected = set(args.aweme_id)
        candidates = [candidate for candidate in candidates if candidate.aweme_id in selected]
    candidates = candidates[: args.max_items] if args.max_items else candidates

    summary_path = run_root / f"summary-{run_id}.json"
    if not candidates:
        summary_path.write_text(
            json.dumps({"status": "empty", "candidate_count": 0}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"No candidates. summary={summary_path}")
        return 0

    print(f"candidates={len(candidates)} runlog={runlog}")
    rows = run_opencli_user_videos(
        args.sec_uid,
        site_session=args.site_session,
        timeout=args.opencli_timeout,
        limit=args.opencli_limit,
    )
    by_id = {row.aweme_id: row for row in rows}
    missing_play_url = [candidate.aweme_id for candidate in candidates if candidate.aweme_id not in by_id]
    success = 0
    failed = 0
    skipped = 0
    for index, candidate in enumerate(candidates, start=1):
        video = by_id.get(candidate.aweme_id)
        if video is None:
            skipped += 1
            write_jsonl(
                runlog,
                {
                    "aweme_id": candidate.aweme_id,
                    "status": "skipped",
                    "path": str(candidate.path),
                    "error": "play_url_not_found_in_opencli_user_videos",
                },
            )
            print(f"[{index}/{len(candidates)}] {candidate.aweme_id} skipped=no_play_url", flush=True)
            continue
        ok = process_candidate(
            candidate,
            video,
            root=root,
            backup_root=backup_root,
            runlog=runlog,
            run_id=run_id,
            ffmpeg_timeout=args.ffmpeg_timeout,
            asr_timeout=args.asr_timeout,
            dry_run=args.dry_run,
        )
        if ok:
            success += 1
            print(f"[{index}/{len(candidates)}] {candidate.aweme_id} success", flush=True)
        else:
            failed += 1
            print(f"[{index}/{len(candidates)}] {candidate.aweme_id} failed", flush=True)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary = {
        "status": "done" if failed == 0 else "partial",
        "root": str(root),
        "candidate_count": len(candidates),
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "missing_play_url_count": len(missing_play_url),
        "runlog": str(runlog),
        "backup_root": str(backup_root),
        "asr_backend": "siliconflow",
        "asr_model": SILICONFLOW_MODEL,
        "opencli_limit": bounded_opencli_limit(args.opencli_limit),
        "media_retention": "none",
        "dry_run": args.dry_run,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Target existing Markdown root")
    parser.add_argument("--sec-uid", required=True, help="Douyin creator sec_uid for OpenCLI user-videos")
    parser.add_argument("--run-root", required=True, help="Run state directory")
    parser.add_argument("--run-id", default="", help="Stable run id for logs/backups")
    parser.add_argument("--site-session", default="persistent", choices=["persistent", "ephemeral"])
    parser.add_argument("--min-meaningful", type=int, default=3)
    parser.add_argument("--min-chars", type=int, default=40)
    parser.add_argument("--max-items", type=int, default=0, help="Limit candidates for smoke")
    parser.add_argument("--aweme-id", action="append", default=[], help="Only process a specific aweme_id; repeatable")
    parser.add_argument("--only-ids", default="", help="JSON file containing aweme IDs or candidate objects")
    parser.add_argument(
        "--opencli-limit",
        type=int,
        default=20,
        help="Recent user-videos rows to inspect; current OpenCLI supports at most 20",
    )
    parser.add_argument("--opencli-timeout", type=int, default=900)
    parser.add_argument("--ffmpeg-timeout", type=int, default=600)
    parser.add_argument("--asr-timeout", type=int, default=300)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ERROR: {sanitize_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
