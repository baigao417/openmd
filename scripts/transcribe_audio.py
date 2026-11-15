"""Local Chinese podcast transcription with a quality-first Whisper profile.

The native platform transcript should remain the first choice. This script is
the local-media fallback when no usable native transcript exists.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

from openmd.adapters.local_audio.backends.faster_whisper import (
    DEFAULT_BEAM_SIZE,
    DEFAULT_HOTWORDS,
    DEFAULT_VAD_MIN_SILENCE_MS,
    FIXED_DECODE_OPTIONS,
)


DEFAULT_PROMPT = (
    "这是中文播客访谈，主题可能包括女性成长、创业、自媒体、财富、投资理财和人工智能。"
    "请保留中文专有名词、英文缩写、数字和人名，不要把正常停顿扩写成内容。"
    "常见词包括：搞钱女孩、大漂亮爱吃肉、梨涡小雨、Leiya、刘六六、Shelly、"
    "小红书、LinkedIn、ETF、SOP、AI。"
)


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"


def clean_text(text: str) -> str:
    return " ".join(str(text).replace("\u3000", " ").split()).strip()


def choose_runtime(device: str, compute_type: str) -> tuple[str, str]:
    if device != "auto":
        selected_device = device
    else:
        try:
            import torch

            selected_device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            selected_device = "cpu"

    if compute_type != "auto":
        return selected_device, compute_type
    return selected_device, "float16" if selected_device == "cuda" else "int8"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe local audio with faster-whisper.")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--json", dest="json_path", type=Path, required=True)
    parser.add_argument("--md", dest="md_path", type=Path, required=True)
    parser.add_argument("--model", default=os.getenv("OPENMD_ASR_MODEL", "large-v3"))
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--compute-type", choices=("auto", "int8", "float16", "float32"), default="auto")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--beam-size", type=int, default=DEFAULT_BEAM_SIZE)
    parser.add_argument("--vad-min-silence-ms", type=int, default=DEFAULT_VAD_MIN_SILENCE_MS)
    parser.add_argument("--initial-prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--hotwords", default=DEFAULT_HOTWORDS)
    parser.add_argument(
        "--condition-on-previous-text",
        action="store_true",
        help="Keep previous-text conditioning; off by default to limit long-form error cascades.",
    )
    return parser.parse_args()


def transcribe(args: argparse.Namespace) -> dict[str, Any]:
    if not args.audio.is_file():
        raise FileNotFoundError(args.audio)

    device, compute_type = choose_runtime(args.device, args.compute_type)
    started = time.time()
    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(args.audio),
        language=args.language,
        task="transcribe",
        beam_size=args.beam_size,
        **FIXED_DECODE_OPTIONS,
        vad_parameters={"min_silence_duration_ms": args.vad_min_silence_ms},
        condition_on_previous_text=args.condition_on_previous_text,
        initial_prompt=args.initial_prompt or None,
        hotwords=args.hotwords or None,
        word_timestamps=False,
    )

    rows: list[dict[str, Any]] = []
    last_report = -1
    for segment in segments:
        text = clean_text(segment.text)
        if not text:
            continue
        row = {"start": round(float(segment.start), 3), "end": round(float(segment.end), 3), "text": text}
        rows.append(row)
        minute = int(row["end"] // 60)
        if minute >= last_report + 5:
            last_report = minute
            print(f"progress_minutes={minute} segments={len(rows)}", flush=True)

    payload: dict[str, Any] = {
        "audio": str(args.audio),
        "model": args.model,
        "device": device,
        "compute_type": compute_type,
        "language": getattr(info, "language", args.language),
        "duration": getattr(info, "duration", None),
        "duration_after_vad": getattr(info, "duration_after_vad", None),
        "segment_count": len(rows),
        "elapsed_seconds": round(time.time() - started, 1),
        "quality_profile": "zh_podcast_v2",
        "condition_on_previous_text": args.condition_on_previous_text,
        "vad_min_silence_duration_ms": args.vad_min_silence_ms,
        "segments": rows,
    }

    args.json_path.parent.mkdir(parents=True, exist_ok=True)
    args.md_path.parent.mkdir(parents=True, exist_ok=True)
    args.json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"{format_timestamp(row['start'])} {row['text']}" for row in rows]
    args.md_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"completed segments={len(rows)} elapsed_seconds={payload['elapsed_seconds']}", flush=True)
    return payload


if __name__ == "__main__":
    transcribe(parse_args())
