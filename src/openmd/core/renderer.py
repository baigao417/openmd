"""Markdown rendering for normalized Openmd extraction results."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .models import ContentItem, MarkdownDocument, TranscriptResult, block_as_dict
from .readable import format_transcript, markdown_label
from .secrets import sanitize_error_summary, sanitize_url_text


FRONTMATTER_FIELDS: tuple[str, ...] = (
    "title",
    "author",
    "url",
    "published",
    "platform",
    "captured_from",
    "coverage",
)

_SOURCE_LABELS = {
    "native_text": "原文",
    "native_caption": "原生字幕",
    "auto_caption": "自动字幕",
    "platform_transcript": "平台逐字稿",
    "third_party_extractor": "第三方提取",
    "browser_rendered": "浏览器提取",
    "manual_export": "手动导出",
    "ocr": "文字识别",
    "asr": "语音识别",
    "media_asr": "语音识别",
    "live_capture_asr": "实时语音识别",
    "partial_extraction": "部分提取",
}

_COVERAGE_LABELS = {
    "provider_declared_complete": "提供方声明完整",
    "visible_branch": "当前可见分支",
    "visible_only": "当前可见页面",
}
_LEGACY_COVERAGE_VALUES = {
    "当前可见分支": "visible_branch",
    "提供方声明完整": "provider_declared_complete",
}


def coverage_machine_value(value: object) -> str:
    machine = str(value or "visible_branch")
    return _LEGACY_COVERAGE_VALUES.get(machine, machine)


def coverage_label(value: object) -> str:
    return _COVERAGE_LABELS.get(coverage_machine_value(value), str(value or "visible_branch"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _frontmatter_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\r", " ").replace("\n", " ")
    # JSON strings are valid YAML scalars and avoid implicit YAML booleans,
    # numbers, dates and special punctuation in external titles/authors.
    return json.dumps(text, ensure_ascii=False)


def source_level_label(value: object) -> str:
    return _SOURCE_LABELS.get(str(value), "提取文本")


def published_date(value: object) -> str:
    """Normalize known timestamp formats; unknown source labels stay intact."""
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if not text:
        return ""
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(text).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return text


def build_source_notes(result: TranscriptResult) -> list[str]:
    notes = [f"backend: {result.backend}", f"source_level: {result.source_level}"]
    for note in result.source_notes:
        if note:
            notes.append(f"notes: {note}")
    if result.error_code:
        notes.append(f"error_code: {result.error_code}")
    if result.error_summary:
        summary = sanitize_error_summary(result.error_summary)
        if summary:
            notes.append(f"error_summary: {summary}")
    return notes


def render_markdown_document(
    item: ContentItem,
    result: TranscriptResult,
    *,
    captured_at: str | None = None,
) -> MarkdownDocument:
    title = result.title or item.title or item.id or "untitled"
    image_post_author_text = (
        item.platform == "douyin" and result.backend == "douyin.post-text"
        and result.source_level == "native_text"
        and result.metadata.get("transcript_method") == "image_post_text"
        and result.metadata.get("media_kind") == "image_text"
        and result.metadata.get("content_kind") == "author_text"
    )
    frontmatter = {
        "platform": item.platform,
        "url": sanitize_url_text(item.url),
        "title": title,
        "author": result.author or item.author,
        "published_at": result.published_at or item.published_at,
        "captured_at": captured_at or utc_now_iso(),
        "source_type": "image_text" if image_post_author_text else item.source_type,
        "image_post_author_text": image_post_author_text,
        "status": result.status,
        "source_level": result.source_level,
        "backend": result.backend,
    }
    if item.platform == "page_snapshot":
        frontmatter.update({
            "captured_from": "browser_snapshot",
            "coverage": "visible_branch",
        })
    if item.source_type == "conversation" or result.blocks:
        frontmatter.update({
            "coverage": coverage_machine_value(result.metadata.get("coverage")),
        })
        if result.blocks:
            content = render_conversation_blocks(
                result.blocks,
                source_url=item.url,
                platform=item.platform,
                exported_at=captured_at or utc_now_iso(),
                coverage=coverage_machine_value(result.metadata.get("coverage")),
                title=str(title),
            )
        else:
            content = result.text
    else:
        content = result.text
    return MarkdownDocument(
        frontmatter=frontmatter,
        title=title,
        content=content,
        source_notes=build_source_notes(result),
    )


def render_conversation_blocks(
    blocks: list[Any],
    *,
    source_url: str,
    platform: str,
    exported_at: str,
    coverage: str = "visible_branch",
    title: str = "对话",
) -> str:
    """Render each conversation turn without merging or summarizing it."""
    lines: list[str] = []
    turn_count = 0
    for block in blocks:
        value = block_as_dict(block)
        kind = value.get("type")
        if kind == "turn":
            turn_count += 1
            role = str(value.get("role") or "unknown")
            timestamp = value.get("time")
            heading = f"### {role}" + (f" · {timestamp}" if timestamp else "")
            lines.extend([heading, "", str(value.get("text") or ""), ""])
        elif kind == "cue":
            lines.extend([f"[{value.get('start')}–{value.get('end')}]", "", str(value.get("text") or ""), ""])
        elif kind == "table":
            rows = value.get("rows") or []
            valid_rows = [row for row in rows if isinstance(row, list)]
            if valid_rows:
                width = max(len(row) for row in valid_rows)
                lines.extend(["| " + " | ".join(str(cell) for cell in valid_rows[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"])
                lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in valid_rows[1:])
                if value.get("merged_cells"):
                    lines.extend(["", "> 表格合并单元格已降级为原始行列标注。"])
                lines.append("")
    lines.extend([
        "---",
        "",
        "来源说明：",
        f"- 平台：{platform}",
        f"- 会话 URL：{sanitize_url_text(source_url)}",
        f"- 导出时刻：{exported_at}",
        f"- 范围：{coverage_label(coverage)}",
        f"- 轮次数：{turn_count}",
        "",
    ])
    return "\n".join(lines)


def _source_footer(document: MarkdownDocument) -> str:
    level = str(document.frontmatter.get("source_level") or "")
    label = "作者原文（图文）" if document.frontmatter.get("image_post_author_text") else source_level_label(level)
    parts = ["来源：" + label]
    if level in {"auto_caption", "ocr", "asr", "media_asr", "live_capture_asr"}:
        parts.append("识别错误保持原样")
    status = document.frontmatter.get("status")
    if status == "partial":
        parts.append("内容可能不完整")
    elif status == "failed":
        parts.append("提取失败")
    if any("识别模型可能自动纠正原文错字" in note for note in document.source_notes):
        parts.append("识别模型可能自动纠正原文错字")
        parts.append("未触发回退不代表无幻觉")
    # Keep user-relevant coverage warnings, not command traces or backend IDs.
    if any("Single post only" in note for note in document.source_notes):
        parts.append("仅此条帖子，未验证整串讨论覆盖")
    if any("视频标题未取得" in note for note in document.source_notes):
        parts.append("视频标题未取得")
    elif any("视频作者未取得" in note for note in document.source_notes):
        parts.append("视频作者未取得")
    failed_images = sum(
        note.startswith(("asset_download_failed:", "notes: asset_download_failed:"))
        for note in document.source_notes
    )
    if failed_images:
        parts.append(f"{failed_images} 张图片下载失败，保留远程链接")
    return "*" + "；".join(parts) + "。*"


def render_document(document: MarkdownDocument, *, raw: bool = False) -> str:
    """Render a reading artifact; ``raw`` preserves the original body layout.

    Structured extraction metadata remains on MarkdownDocument for runlogs and
    package consumers; only the five human-facing source fields are printed.
    """
    lines = ["---"]
    for field in FRONTMATTER_FIELDS:
        value = document.frontmatter.get(field)
        if field == "title":
            value = document.title
        if field == "published":
            value = published_date(value or document.frontmatter.get("published_at"))
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        lines.append(f"{field}: {_frontmatter_value(value)}")
    content = document.content if raw or document.frontmatter.get("image_post_author_text") else format_transcript(
        document.content,
        source_type=document.frontmatter.get("source_type"),
        platform=document.frontmatter.get("platform"),
    )
    heading = f"# {markdown_label(document.title)}"
    if not raw and document.frontmatter.get("platform") != "x" and not document.frontmatter.get("image_post_author_text"):
        # Source extractors may repeat a title as the first paragraph/heading.
        # X post titles are derived from the body, so keep their first sentence.
        # Promote only an exact match for sources with independent titles.
        first, separator, rest = content.lstrip("\r\n").partition("\n")
        if first.strip() in {document.title, "# " + document.title}:
            content = rest.lstrip("\r\n") if separator else ""
    prefix = "\n".join([*lines, "---", "", heading, "", ""])
    # Do not rstrip raw content: trailing spaces/newlines can be meaningful.
    return prefix + content + ("" if content.endswith("\n") else "\n") + "\n" + _source_footer(document) + "\n"
