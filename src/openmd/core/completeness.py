"""Independent, monotonic content checks. Unknown evidence never certifies text.

Checkers receive the item, extraction result and already available independent
signals. No checker fetches network metadata or acquires additional permissions.
"""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from .models import ContentItem, TranscriptResult


@dataclass(frozen=True)
class Check:
    signal: str
    status: str = "unknown"
    reason: str | None = None
    gap: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "status": self.status, "reason": self.reason, "gap": self.gap}


Checker = Callable[[ContentItem, TranscriptResult, Mapping[str, Any]], Check]
_TIMESTAMP = r"\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d+)?"
_CUE_END = re.compile(rf"{_TIMESTAMP}\s*-->\s*({_TIMESTAMP})")
_BRACKET_CUE_END = re.compile(rf"\[{_TIMESTAMP}\s*-\s*({_TIMESTAMP})\]")


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and value.startswith("PT"):
            match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value)
            return sum(float(part or 0) * unit for part, unit in zip(match.groups(), (3600, 60, 1))) if match else None
        parts = str(value).replace(",", ".").split(":")
        number = 0.0
        for part in parts:
            number = number * 60 + float(part)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _local_path(item: ContentItem):
    from openmd.adapters.local_audio.adapter import local_path_from_input

    return local_path_from_input(str(item.raw_meta.get("local_path") or item.url))


def available_signals(item: ContentItem, result: TranscriptResult) -> dict[str, Any]:
    """Prefer existing platform metadata; probe only the explicit local source."""
    signals: dict[str, Any] = {}
    if item.source_type == "conversation":
        for value in (item.raw_meta.get("declared_turn_count"), result.metadata.get("independent_turn_count")):
            if type(value) is int and value >= 0:
                signals["conversation_turn_count"] = value
                break
    if item.source_type in {"audio", "video"} or result.source_level in {"asr", "media_asr", "native_caption", "auto_caption", "platform_transcript"}:
        coverage = result.metadata.get("coverage")
        coverage = coverage if isinstance(coverage, dict) else {}
        for value in (item.raw_meta.get("duration_seconds"), item.raw_meta.get("duration"),
                      coverage.get("duration_seconds")):
            duration = _seconds(value)
            if duration is not None and duration > 0:
                signals["media_duration_seconds"] = duration
                break
        if "media_duration_seconds" not in signals:
            path = _local_path(item)
            if path and path.is_file():
                try:
                    from openmd.runtime.interpreters import run_interruptibly

                    done = run_interruptibly(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                                             stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)
                    duration = _seconds(done.stdout.decode() if isinstance(done.stdout, bytes) else done.stdout)
                    if duration is not None and duration > 0:
                        signals["media_duration_seconds"] = duration
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
        if "media_duration_seconds" not in signals:
            for key in ("duration_seconds", "duration"):
                duration = _seconds(result.metadata.get(key))
                if duration is not None and duration > 0:
                    signals["media_duration_seconds"] = duration
                    break
    if item.platform == "pdf":
        path = _local_path(item)
        if path and path.is_file() and path.suffix.lower() == ".pdf":
            try:
                from openmd.ocr.documents import pymupdf

                with pymupdf().open(path) as document:
                    if not document.needs_pass:
                        signals["pdf_page_count"] = len(document)
            except (ImportError, OSError, ValueError, RuntimeError):
                pass
    return signals


def media_cue_check(item: ContentItem, result: TranscriptResult, signals: Mapping[str, Any]) -> Check:
    duration = _seconds(signals.get("media_duration_seconds"))
    # End timestamps only: a chunk's start is not evidence of its end. In
    # particular Qwen's start-only lines must remain unknown without cue ends.
    ends = [_seconds(match.group(1)) for pattern in (_CUE_END, _BRACKET_CUE_END) for match in pattern.finditer(result.text)]
    ends = [value for value in ends if value is not None]
    if not ends:
        coverage_end = _seconds(result.metadata.get("coverage_end_seconds"))
        if coverage_end is not None:
            ends.append(coverage_end)
        for cue in result.metadata.get("cues", []) if isinstance(result.metadata.get("cues"), list) else []:
            if isinstance(cue, dict) and (end := _seconds(cue.get("end"))) is not None:
                ends.append(end)
    if duration is None or duration <= 0 or not ends:
        return Check("media_duration", reason="duration_or_cue_end_unavailable")
    end = max(ends)
    if duration - end > max(60, duration * 0.05):
        return Check("media_duration", "downgrade", "transcript_ends_early", {"start_seconds": end, "end_seconds": duration})
    return Check("media_duration", "ok")


def pdf_page_check(item: ContentItem, result: TranscriptResult, signals: Mapping[str, Any]) -> Check:
    count = signals.get("pdf_page_count")
    pages = result.metadata.get("pages")
    if type(count) is not int or count <= 0 or not isinstance(pages, list) or result.metadata.get("page_mapping") == "not_available":
        return Check("pdf_pages", reason="page_count_or_mapping_unavailable")
    numbered = [page for page in pages if isinstance(page, dict) and type(page.get("page")) is int]
    verified = {page["page"] for page in numbered if page.get("status") == "verified"}
    # Text-layer pages whose visibility could not be proven are still output;
    # they are reported as unverified, not as missing.
    unverified = {page["page"] for page in numbered if page.get("textlayer_unverified") is True and page["page"] not in verified}
    # A text-layer page that only had hidden characters removed is still output.
    filtered = {page["page"] for page in numbered if page.get("route") == "textlayer" and page.get("status") in {"verified", "partial"}}
    present = verified | unverified | filtered
    missing = sorted(set(range(1, count + 1)) - present)
    if missing:
        return Check(
            "pdf_pages",
            "downgrade",
            "pdf_pages_missing",
            {"missing_pages": missing, "output_pages": len(present), "total_pages": count,
             **({"unverified_pages": len(unverified)} if unverified else {})},
        )
    if unverified:
        return Check(
            "pdf_pages",
            "downgrade",
            "pdf_textlayer_unverified",
            {"missing_pages": [], "output_pages": len(present), "unverified_pages": len(unverified), "total_pages": count},
        )
    return Check("pdf_pages", "ok")


def conversation_count_check(item: ContentItem, result: TranscriptResult, signals: Mapping[str, Any]) -> Check:
    """Compare an export count with an independent conversation statistic.

    Providers may supply ``exported_count``, ``stats_count`` and ``limit_hit``
    in result metadata. Unknown values stay unknown; a proven short export or
    a hit limit only downgrades a previously verified result.
    """
    if item.source_type != "conversation":
        return Check("conversation_count", reason="not_a_conversation")
    exported = result.metadata.get("exported_count")
    stats_count = result.metadata.get("stats_count")
    limit = result.metadata.get("limit")
    if result.metadata.get("limit_hit") is True:
        return Check(
            "conversation_count",
            "downgrade",
            "export_limit_reached",
            {
                "exported_count": exported,
                "limit": limit,
                "next_action": "increase export limit or narrow the time range",
            },
        )
    if type(exported) is int and type(stats_count) is int:
        if exported < stats_count:
            return Check(
                "conversation_count",
                "downgrade",
                "export_count_below_stats",
                {"exported_count": exported, "stats_count": stats_count},
            )
        if exported == stats_count:
            return Check("conversation_count", "ok")
        return Check(
            "conversation_count",
            "downgrade",
            "export_count_above_stats",
            {"exported_count": exported, "stats_count": stats_count},
        )
    return Check("conversation_count", reason="export_or_stats_count_unavailable")


def check_completeness(item: ContentItem, result: TranscriptResult, *, signals: Mapping[str, Any] | None = None,
                       checkers: tuple[Checker, ...] | None = None) -> TranscriptResult:
    """Attach bounded evidence and lower verified to partial when a gap is proven."""
    selected = checkers
    if selected is None:
        selected = (pdf_page_check,) if item.platform == "pdf" else ((media_cue_check,) if item.source_type in {"audio", "video"} or result.source_level in {"asr", "media_asr", "native_caption", "auto_caption", "platform_transcript"} else ())
        if item.source_type == "conversation":
            selected = (*selected, conversation_count_check, conversation_turn_check)
    if not selected:
        return result
    evidence = available_signals(item, result) if signals is None else signals
    checks = [checker(item, result, evidence) for checker in selected]
    # Any proven gap downgrades; otherwise one independent proof suffices while
    # the remaining checks merely lack evidence (unknown), never contradict it.
    status = "downgrade" if any(check.status == "downgrade" for check in checks) else (
        "ok" if any(check.status == "ok" for check in checks) and all(check.status in {"ok", "unknown"} for check in checks) else "unknown")
    notes = [*result.source_notes]
    for check in checks:
        if check.status == "downgrade":
            note = f"Independent completeness: {check.reason}; gap={check.gap}"
            if note not in notes:
                notes.append(note)
    return replace(result, status="partial" if status == "downgrade" and result.status == "verified" else result.status,
                   source_notes=notes, metadata={**result.metadata, "completeness": {"status": status, "checks": [check.as_dict() for check in checks]}})


def conversation_turn_check(item: ContentItem, result: TranscriptResult, signals: Mapping[str, Any]) -> Check:
    expected_users = result.metadata.get("independent_user_turn_count")
    actual_users = sum(1 for block in result.blocks if (block.get("role") if isinstance(block, dict) else getattr(block, "role", None)) == "user")
    if type(expected_users) is int and actual_users < expected_users:
        return Check("conversation_turns", "downgrade", "conversation_user_turns_missing", {"exported_user_turns": actual_users, "expected_user_turns": expected_users})
    if result.metadata.get("conversation_user_evidence_unavailable") is True:
        return Check("conversation_turns", reason="conversation_user_evidence_unavailable")
    expected = signals.get("conversation_turn_count")
    actual = sum(1 for block in result.blocks if (getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)) == "turn")
    if type(expected) is not int or expected < 0 or not result.blocks:
        return Check("conversation_turns", reason="independent_turn_count_unavailable")
    if actual < expected:
        return Check("conversation_turns", "downgrade", "conversation_turns_missing", {"exported_turns": actual, "expected_turns": expected, "missing_turns": expected - actual})
    return Check("conversation_turns", "ok")
