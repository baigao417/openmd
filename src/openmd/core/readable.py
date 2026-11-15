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
# Caption tables with second offsets, e.g. "| 3 | 16.64s | 20.37s | text |".
_SECONDS_HEADER = re.compile(r"^\s*\|\s*index\s*\|\s*start\s*\|\s*end\s*\|\s*text\s*\|\s*$", re.I)
_SECONDS_SEPARATOR = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|){4}\s*$")
_SECONDS_ROW = re.compile(
    r"^\s*\|\s*\d+\s*\|\s*(?P<start>\d+(?:\.\d+)?)s\s*\|\s*\d+(?:\.\d+)?s\s*\|(?P<text>.*)\|\s*$"
)
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#!|])")
_SENTENCE_END = re.compile(r"[.!?。！？][\"'’”）)]*$")
_UNKNOWN_LAYOUT_MIN_LINES = 20


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


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    return f"{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"


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


def _seconds_table_captions(text: str) -> list[_Caption] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or not _SECONDS_HEADER.fullmatch(lines[0]) or not _SECONDS_SEPARATOR.fullmatch(lines[1]):
        return None
    captions = []
    for line in lines[2:]:
        match = _SECONDS_ROW.fullmatch(line)
        if match is None:
            return None
        content = match.group("text").strip()
        if content:
            captions.append(_Caption(_clock(float(match.group("start"))), content))
    return captions or None


def _line_captions(text: str) -> list[_Caption] | None:
    """Parse timestamped lines, appending untimed lines to the prior caption."""
    captions: list[_Caption] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CAPTION.fullmatch(line)
        if match is not None:
            captions.append(_Caption(match.group("time"), match.group("text")))
        elif captions:
            last = captions[-1]
            captions[-1] = _Caption(last.timestamp, f"{last.text} {line.strip()}", last.speaker, last.chapter)
        else:
            return None
    return captions or None


def _one_paragraph_per_line(text: str) -> str:
    return "\n\n".join(line for line in text.splitlines() if line.strip())


def format_transcript(text: str, *, source_type: str | None, platform: str | None = None) -> str:
    """Group recognized captions only; articles and mixed/unknown formats pass through.

    No text is summarized, corrected, sorted, deduplicated or repunctuated.
    Recognized layouts include timestamped lines and caption tables. Unknown
    long transcripts get paragraph breaks between source lines; other text passes through.
    """
    if source_type not in {"video", "audio"}:
        return text
    lines = text.splitlines()
    lead = 0
    while lead < len(lines) and (not lines[lead].strip() or lines[lead].lstrip().startswith("# ")):
        lead += 1
    preface = "\n".join(line for line in lines[:lead] if line.strip())
    if preface:
        body = format_transcript("\n".join(lines[lead:]), source_type=source_type, platform=platform)
        return f"{preface}\n\n{body}" if body.strip() else text
    captions = _table_captions(text)
    table = captions is not None
    if captions is None:
        captions = _seconds_table_captions(text)
    if captions is None:
        captions = _line_captions(text)
    if captions is None:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) >= _UNKNOWN_LAYOUT_MIN_LINES and "\n\n" not in text.strip():
            return _one_paragraph_per_line(text)
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
                short = len(group[-1].text) < 40
                boundary = group_chars >= 450 or duration >= (30 if short else 90) or (short and gap >= 6)
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
