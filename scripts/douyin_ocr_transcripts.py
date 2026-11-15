"""Extract Douyin hard-subtitle transcripts with OpenCLI + ffmpeg + OCR.

This helper is intentionally outside the default Openmd backend path because it
uses temporary video frames. It does not download whole videos, persist play
URLs, save raw API responses, or keep frames after OCR.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z+&./:_()'-]{1,}")
SPACE_RE = re.compile(r"\s+")
MAX_OPENCLI_DOUYIN_LIMIT = 20


def sanitize_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(r"(token|cookie|authorization|signature|x-signature)=([^&\s]+)", r"\1=[redacted]", text, flags=re.I)
    return text[:500]


@dataclass(frozen=True)
class VideoRow:
    aweme_id: str
    title: str
    duration: int
    play_url: str


def _command_path(name: str) -> str:
    resolved = shutil.which(name) or shutil.which(f"{name}.cmd")
    if not resolved:
        raise RuntimeError(f"Required command not found on PATH: {name}")
    return resolved


def _run(
    command: list[str],
    *,
    timeout: int,
    encoding: str = "utf-8",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding=encoding,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def fetch_opencli_rows(
    sec_uid: str,
    *,
    limit: int | None,
    all_items: bool,
    site_session: str,
    timeout: int,
) -> list[VideoRow]:
    if all_items:
        raise RuntimeError(
            "current OpenCLI douyin user-videos supports only --limit (maximum 20); "
            "full pagination and video-detail are unavailable"
        )
    opencli = _command_path("opencli")
    bounded_limit = max(1, min(int(limit or MAX_OPENCLI_DOUYIN_LIMIT), MAX_OPENCLI_DOUYIN_LIMIT))
    command = [
        opencli,
        "douyin",
        "user-videos",
        sec_uid,
        "--limit",
        str(bounded_limit),
        "--with_comments",
        "false",
        "--site-session",
        site_session,
        "-f",
        "json",
    ]

    completed = _run(command, timeout=timeout)
    if completed.returncode != 0:
        summary = (completed.stderr or completed.stdout or "").strip()[:500]
        raise RuntimeError(f"opencli douyin user-videos failed: exit={completed.returncode} {summary}")

    payload = json.loads(completed.stdout)
    rows = payload if isinstance(payload, list) else payload.get("data") or payload.get("items") or []
    parsed: list[VideoRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        aweme_id = str(row.get("aweme_id") or "").strip()
        play_url = str(row.get("play_url") or "").strip()
        if not aweme_id or not play_url:
            continue
        try:
            duration = int(float(row.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        parsed.append(
            VideoRow(
                aweme_id=aweme_id,
                title=str(row.get("title") or aweme_id).strip() or aweme_id,
                duration=max(0, duration),
                play_url=play_url,
            )
        )
    return parsed


def read_manifest_rows(path: Path) -> list[VideoRow]:
    rows: list[VideoRow] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        aweme_id = str(row.get("video_id") or row.get("aweme_id") or row.get("id") or "").strip()
        if not aweme_id:
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
                play_url=str(row.get("play_url") or "").strip(),
            )
        )
    return rows


def safe_filename(value: str, *, max_len: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value).strip(" ._")
    text = SPACE_RE.sub(" ", text)
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or "untitled"


def normalize_text(value: str) -> str:
    return SPACE_RE.sub("", value).strip()


def clean_ocr_text(value: str) -> str:
    text = SPACE_RE.sub("", value)
    text = ASCII_WORD_RE.sub("", text)
    text = re.sub(r"[^\u4e00-\u9fff0-9%，。！？、：；（）《》“”+-]", "", text)
    text = re.sub(r"[+]{2,}", "", text)
    return text.strip("，。！？、：；+-")


def box_xyxy(box: Any) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in box]
    ys = [float(point[1]) for point in box]
    return min(xs), min(ys), max(xs), max(ys)


def ocr_frame(ocr: Any, image_path: Path, *, min_score: float) -> str:
    result, _elapsed = ocr(str(image_path))
    if not result:
        return ""

    items: list[tuple[int, float, str]] = []
    for box, text, score in result:
        cleaned = clean_ocr_text(str(text or ""))
        if score < min_score or not cleaned or not CHINESE_RE.search(cleaned):
            continue
        x1, y1, _x2, _y2 = box_xyxy(box)
        # Bucket close OCR boxes into the same visual line, then order left-to-right.
        line_bucket = int(round(y1 / 28))
        items.append((line_bucket, x1, cleaned))
    items.sort(key=lambda item: (item[0], item[1]))
    return "".join(text for _line, _x, text in items)


def extract_frames(
    video: VideoRow,
    temp_dir: Path,
    *,
    sample_interval: float,
    max_seconds: int | None,
    crop: str,
    timeout: int,
) -> list[Path]:
    ffmpeg = _command_path("ffmpeg")
    seconds = video.duration or max_seconds or 60
    if max_seconds is not None:
        seconds = min(seconds, max_seconds)
    fps = 1.0 / sample_interval
    pattern = temp_dir / "frame_%05d.jpg"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(max(1, int(seconds))),
        "-i",
        video.play_url,
        "-vf",
        f"fps={fps:g},{crop}",
        "-q:v",
        "3",
        str(pattern),
    ]
    completed = _run(command, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {completed.stderr.strip()[:500]}")
    return sorted(temp_dir.glob("frame_*.jpg"))


def segments_from_frames(
    video: VideoRow,
    *,
    ocr: Any,
    sample_interval: float,
    max_seconds: int | None,
    crop: str,
    min_score: float,
    similarity_threshold: float,
    timeout: int,
) -> tuple[list[tuple[float, str]], int]:
    with tempfile.TemporaryDirectory(prefix="openmd-douyin-ocr-") as tmp:
        temp_dir = Path(tmp)
        frames = extract_frames(
            video,
            temp_dir,
            sample_interval=sample_interval,
            max_seconds=max_seconds,
            crop=crop,
            timeout=timeout,
        )
        segments: list[tuple[float, str]] = []
        last_norm = ""
        for index, frame in enumerate(frames):
            text = ocr_frame(ocr, frame, min_score=min_score)
            norm = normalize_text(text)
            if not norm:
                continue
            similarity = difflib.SequenceMatcher(None, norm, last_norm).ratio() if last_norm else 0.0
            if similarity < similarity_threshold:
                segments.append((index * sample_interval, text))
                last_norm = norm
        return segments, len(frames)


def write_transcript(
    out_dir: Path,
    video: VideoRow,
    segments: list[tuple[float, str]],
    *,
    sample_interval: float,
    crop: str,
) -> Path:
    transcripts_dir = out_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    name = f"{video.aweme_id}__{safe_filename(video.title)}.md"
    path = transcripts_dir / name
    lines = [
        f"# {video.title}",
        "",
        "## Source",
        "",
        "- Platform: douyin",
        f"- Video ID: {video.aweme_id}",
        f"- URL: https://www.douyin.com/video/{video.aweme_id}",
        "",
        "## Transcript",
        "",
    ]
    if segments:
        for seconds, text in segments:
            lines.append(f"[{int(seconds):02d}s] {text}")
    else:
        lines.append("_No hard-subtitle text was detected by OCR._")
    lines.extend(
        [
            "",
            "## Method",
            "",
            "- Source path: OpenCLI Douyin logged-in browser context for temporary playback URL.",
            "- Extraction: ffmpeg sampled temporary frames; RapidOCR recognized visible hard subtitles.",
            "- Retention: no full video, raw API response, playback URL, cookie, token, or frame files are retained.",
            f"- Sample interval: {sample_interval:g}s",
            f"- Crop: `{crop}`",
            "- ASR: not used.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def append_runlog(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_only_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    rows = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
    ids: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            value = row.strip()
        elif isinstance(row, dict):
            value = str(row.get("aweme_id") or row.get("awemeId") or row.get("video_id") or "").strip()
        else:
            value = ""
        if value:
            ids.add(value)
    return ids


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    runlog = out_dir / "runlog.jsonl"

    if args.manifest:
        rows = read_manifest_rows(Path(args.manifest))
    else:
        rows = fetch_opencli_rows(
            args.sec_uid,
            limit=args.limit,
            all_items=args.all,
            site_session=args.site_session,
            timeout=args.opencli_timeout,
        )
    only_ids = load_only_ids(Path(args.only_ids)) if args.only_ids else None
    if only_ids is not None:
        rows = [row for row in rows if row.aweme_id in only_ids]
    if args.max_items is not None:
        rows = rows[: args.max_items]
    processed = 0
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    for index, video in enumerate(rows, start=1):
        target_name = f"{video.aweme_id}__{safe_filename(video.title)}.md"
        target = out_dir / "transcripts" / target_name
        if target.exists() and args.skip_existing:
            append_runlog(
                runlog,
                {
                    "aweme_id": video.aweme_id,
                    "status": "skipped",
                    "reason": "skip_existing",
                    "path": str(target),
                    "ts": time.time(),
                },
            )
            continue
        try:
            if not video.play_url:
                raise RuntimeError(
                    "missing play_url; current OpenCLI has no supported video-detail command, "
                    "so use a bounded user-videos row or an explicit transient manifest containing play_url"
                )
            segments, frame_count = segments_from_frames(
                video,
                ocr=ocr,
                sample_interval=args.sample_interval,
                max_seconds=args.max_seconds,
                crop=args.crop,
                min_score=args.min_score,
                similarity_threshold=args.similarity_threshold,
                timeout=args.ffmpeg_timeout,
            )
            path = write_transcript(
                out_dir,
                video,
                segments,
                sample_interval=args.sample_interval,
                crop=args.crop,
            )
            append_runlog(
                runlog,
                {
                    "aweme_id": video.aweme_id,
                    "title": video.title,
                    "status": "ok" if segments else "no_text_detected",
                    "segments": len(segments),
                    "frames": frame_count,
                    "path": str(path),
                    "method": "hard_subtitle_ocr",
                    "asr": False,
                    "raw_media_retained": False,
                    "ts": time.time(),
                },
            )
            print(f"[{index}/{len(rows)}] {video.aweme_id} segments={len(segments)} path={path}", flush=True)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - CLI runlog must continue per item.
            append_runlog(
                runlog,
                {
                    "aweme_id": video.aweme_id,
                    "title": video.title,
                    "status": "failed",
                    "error": sanitize_error(exc),
                    "method": "hard_subtitle_ocr",
                    "asr": False,
                    "raw_media_retained": False,
                    "ts": time.time(),
                },
            )
            print(f"[{index}/{len(rows)}] {video.aweme_id} failed: {sanitize_error(exc)}", flush=True)
            if args.fail_fast:
                return 1
    print(f"processed={processed} total={len(rows)} out={out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sec-uid", default="")
    parser.add_argument(
        "--manifest",
        default="",
        help="Explicit transient JSONL rows; rows must include play_url because OpenCLI has no video-detail command",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--only-ids", default="", help="JSON file containing aweme IDs or row objects to include")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Unsupported by current OpenCLI; retained only to fail closed with a clear error",
    )
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--crop", default="crop=iw:ih*0.52:0:ih*0.45")
    parser.add_argument("--min-score", type=float, default=0.55)
    parser.add_argument("--similarity-threshold", type=float, default=0.82)
    parser.add_argument("--site-session", default="persistent", choices=["persistent", "ephemeral"])
    parser.add_argument("--skip-existing", action="store_true", default=True)
    parser.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--opencli-timeout", type=int, default=900)
    parser.add_argument("--ffmpeg-timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.sec_uid and not args.manifest:
        parser.error("one of --sec-uid or --manifest is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
