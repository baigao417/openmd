"""Conservative display formatting for caption text, without rewriting words."""

from __future__ import annotations

import re
from dataclasses import dataclass


_TIME = r"\d{1,}:[0-5]\d(?::[0-5]\d)?"
# Local ASR emits "[start-end] text"; a caption keeps only its start time for display.
_CAPTION = re.compile(rf"^\[(?P<time>{_TIME})(?:-{_TIME})?\][ \t]+(?P<text>.+)$")
_TABLE_ROW = re.compile(
    rf"^\s*\|\s*(?P<time>{_TIME})\s*\|(?P<speaker>[^|]*)\|(?P<text>.*)\|\s*$"
)
_TABLE_HEADER = re.compile(r"^\s*\|\s*timestamp\s*\|\s*speaker\s*\|\s*text\s*\|\s*$", re.I)
_TABLE_SEPARATOR = re.compile(r"^\s*\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|\s*$")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#!|])")
_SENTENCE_END = re.compile(r"[.!?。！？][\"'’”）)]*$")


def markdown_label(value: object) -> str:
    """Escape external text used in a generated heading or label."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


@dataclass(frozen=True)
class _Caption:
    timestamp: str
    text: str
    speaker: str = ""
    chapter: bool = False

    @property
    def seconds(self) -> int:
        total = 0
        for part in self.timestamp.split(":"):
            total = total * 60 + int(part)
        return total


def _table_captions(text: str) -> list[_Caption] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or not _TABLE_HEADER.fullmatch(lines[0]) or not _TABLE_SEPARATOR.fullmatch(lines[1]):
        return None
    captions = []
    for line in lines[2:]:
        match = _TABLE_ROW.fullmatch(line)
        if match is None:
            return None
        content = match.group("text").strip()
        if not content:
            return None
        captions.append(_Caption(match.group("time"), content, match.group("speaker").strip(), content.startswith("[Chapter] ")))
    return captions


def _line_captions(text: str) -> list[_Caption] | None:
    captions = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CAPTION.fullmatch(line)
        if match is None:
            return None
        captions.append(_Caption(match.group("time"), match.group("text")))
    return captions or None


def format_transcript(text: str, *, source_type: str | None, platform: str | None = None) -> str:
    """Group recognized captions only; articles and mixed/unknown formats pass through.

    No text is summarized, corrected, sorted, deduplicated or repunctuated.
    Only exact transcript table/line formats are recognized. A partly matching
    document is returned intact rather than dropping unfamiliar source lines.
    """
    if source_type not in {"video", "audio"}:
        return text
    captions = _table_captions(text)
    table = captions is not None
    if captions is None:
        captions = _line_captions(text)
    if captions is None:
        return text

    blocks: list[str] = []
    group: list[_Caption] = []
    group_chars = 0

    def flush() -> None:
        if not group:
            return
        first = group[0]
        label = f"{markdown_label(first.speaker)} ({first.timestamp})" if first.speaker else first.timestamp
        blocks.append(f"**{label}**\n\n" + " ".join(caption.text for caption in group))

    for caption in captions:
        if caption.chapter:
            flush()
            group = []
            group_chars = 0
            heading = caption.text[len("[Chapter] "):]
            blocks.append(f"## {markdown_label(heading)} ({caption.timestamp})")
            continue
        if group:
            duration = caption.seconds - group[0].seconds
            gap = caption.seconds - group[-1].seconds
            if table:
                # Use source timestamps to bound long turns even when captions
                # have no sentence-ending punctuation. Never split row text.
                boundary = (
                    caption.speaker != group[0].speaker
                    or duration >= 60
                    or group_chars >= 2400
                    or (group_chars >= 1200 and bool(_SENTENCE_END.search(group[-1].text.rstrip())))
                )
            else:
                boundary = duration >= 30 or gap >= 6 or group_chars >= 450
            if gap < 0:
                # Retain source order, even if upstream times move backwards.
                boundary = True
            if boundary:
                flush()
                group = []
                group_chars = 0
        group.append(caption)
        group_chars += len(caption.text)
    flush()
    return "\n\n".join(blocks)
