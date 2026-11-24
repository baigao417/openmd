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
    "format",
    "completeness",
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


def _demote_turn_headings(text: str) -> str:
    """Keep turn headings above Markdown headings in the original turn body."""
    lines = text.splitlines(keepends=True)
    min_level: int | None = None
    fence: str | None = None
    for line in lines:
        stripped = line.lstrip(" ")
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            continue
        if stripped.rstrip("\r\n") in {"#### 思考过程", "#### 最终回答"}:
            continue
        match = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+.*|[ \t]*)$", line.rstrip("\r\n"))
        if match:
            level = len(match.group(1))
            min_level = level if min_level is None else min(min_level, level)

    offset = max(0, 4 - min_level) if min_level is not None else 0
    output: list[str] = []
    fence: str | None = None
    for line in lines:
        stripped = line.lstrip(" ")
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is not None:
            output.append(line)
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            output.append(line)
            continue
        if stripped.rstrip("\r\n") in {"#### 思考过程", "#### 最终回答"}:
            output.append(line)
            continue
        match = re.match(r"^( {0,3})(#{1,6})([ \t]+.*|[ \t]*)$", line.rstrip("\r\n"))
        if match:
            level = min(6, len(match.group(2)) + offset)
            ending = line[len(line.rstrip("\r\n")):]
            output.append(match.group(1) + ("#" * level) + match.group(3) + ending)
        else:
            output.append(line)
    return "".join(output)

_COVERAGE_LABELS = {
    "provider_declared_complete": "提供方声明完整",
    "visible_branch": "当前可见分支",
    "visible_only": "当前可见页面",
    "full_page": "整页内容",
    "scrolled_full": "滚动采集完整页面",
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


def _format_completeness_seconds(value: Any) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    whole = int(round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _completeness_frontmatter(result: TranscriptResult) -> str | None:
    """Render only proven gaps as a short human-facing front matter scalar."""
    completeness = result.metadata.get("completeness")
    if not isinstance(completeness, dict) or completeness.get("status") != "downgrade":
        return None
    checks = completeness.get("checks")
    if not isinstance(checks, list):
        return "partial — 独立完整度核对发现缺口"
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "downgrade":
            continue
        gap = check.get("gap")
        if check.get("signal") == "media_duration" and isinstance(gap, dict):
            start = _format_completeness_seconds(gap.get("start_seconds"))
            end = _format_completeness_seconds(gap.get("end_seconds"))
            if start and end:
                return f"partial — 逐字稿止于 {start}，媒体时长 {end}"
        if check.get("signal") == "pdf_pages" and isinstance(gap, dict):
            unverified = gap.get("unverified_pages")
            unverified = unverified if isinstance(unverified, int) and unverified > 0 else 0
            note = f"，其中 {unverified} 页文字层未核验" if unverified else ""
            total = gap.get("total_pages")
            if not gap.get("missing_pages") and unverified and isinstance(total, int) and total > 0:
                return f"partial — 已输出 {total}/{total} 页{note}"
            missing = gap.get("missing_pages")
            if isinstance(missing, list) and missing:
                numbers = [page for page in missing if isinstance(page, int) and page > 0]
                if numbers:
                    total = gap.get("total_pages")
                    total = total if isinstance(total, int) and total > 0 else max(numbers)
                    output = gap.get("output_pages")
                    output = output if isinstance(output, int) and output >= 0 else max(0, total - len(numbers))
                    return f"partial — 输出 {output}/{total} 页{note}"
        if check.get("signal") == "snapshot_coverage":
            gap = gap if isinstance(gap, dict) else {}
            collected, declared = gap.get("collected_count"), gap.get("declared_total")
            count = f"（约 {collected}/{declared}）" if type(collected) is int and type(declared) is int else ""
            return f"partial — 只保存了屏幕上的部分{count}"
        reason = str(check.get("reason") or "独立完整度核对发现缺口").strip()
        return f"partial — {reason}"
    return "partial — 独立完整度核对发现缺口"


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
    if result.metadata.get("captured_from") == "browser_snapshot" or result.backend.startswith("page_snapshot."):
        capture = result.metadata.get("capture")
        if isinstance(capture, dict):
            collected, declared = capture.get("collected_count"), capture.get("declared_total")
            count = f"（约 {collected}/{declared}）" if type(collected) is int and type(declared) is int else ""
        else:
            count = ""
        completeness = result.metadata.get("completeness")
        state = completeness.get("status") if isinstance(completeness, dict) else None
        if state == "downgrade":
            notes.append(f"只保存了屏幕上的部分{count}。")
        elif state == "unknown":
            notes.append("页面覆盖范围无法确认；请检查是否还有未采集内容。")
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
    from .input_validation import normalize_http_url

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
        "url": sanitize_url_text(normalize_http_url(item.url)),
        "title": title,
        "author": result.author or item.author,
        "published_at": result.published_at or item.published_at,
        "captured_at": captured_at or utc_now_iso(),
        "source_type": "image_text" if image_post_author_text else item.source_type,
        "image_post_author_text": image_post_author_text,
        "status": result.status,
        "source_level": result.source_level,
        "backend": result.backend,
        "format": document_source_format(item, result),
    }
    completeness = _completeness_frontmatter(result)
    if completeness is not None:
        frontmatter["completeness"] = completeness
    if item.platform == "page_snapshot" or result.metadata.get("captured_from") == "browser_snapshot":
        capture = result.metadata.get("capture")
        coverage = capture.get("coverage") if isinstance(capture, dict) else None
        completeness_status = (result.metadata.get("completeness") or {}).get("status") if isinstance(result.metadata.get("completeness"), dict) else None
        if coverage not in {"full_page", "scrolled_full", "visible_only"}:
            # No capture receipt: a single read of the rendered DOM is what was saved.
            coverage = "visible_only"
        frontmatter.update({
            "captured_from": "browser_snapshot",
            "coverage": coverage,
        })
        if completeness_status == "unknown":
            frontmatter["completeness"] = "unknown — 页面覆盖范围无法确认"
        elif completeness_status == "downgrade" and "completeness" not in frontmatter:
            frontmatter["completeness"] = "partial — 只保存了屏幕上的部分"
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
                source_notes=[note for note in result.source_notes if note.startswith(("已省略思考过程：", "已保留思考过程：", "已省略内嵌图片：", "已删除内嵌小图标：", "公式可能失真："))],
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


def document_source_format(item: ContentItem, result: TranscriptResult) -> str | None:
    if item.platform != "pdf" or item.source_type != "document":
        return None
    from pathlib import Path
    from urllib.parse import unquote, urlparse

    raw = item.raw_meta.get("local_path") or unquote(urlparse(item.url).path)
    suffix = Path(str(raw)).suffix.lstrip(".").lower()
    value = suffix or str(result.metadata.get("source_format") or "").lower()
    return value if re.fullmatch(r"[a-z0-9]{1,12}", value) else None
def render_conversation_blocks(
    blocks: list[Any],
    *,
    source_url: str,
    platform: str,
    exported_at: str,
    coverage: str = "visible_branch",
    title: str = "对话",
    source_notes: list[str] | None = None,
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
            lines.extend([heading, "", _demote_turn_headings(str(value.get("text") or "")), ""])
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
        *[f"- {note}" for note in source_notes or []],
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
        rendered = str(value) if field == "format" and re.fullmatch(r"[a-z0-9]{1,12}", str(value)) else _frontmatter_value(value)
        lines.append(f"{field}: {rendered}")
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
    source_format = document.frontmatter.get("format")
    source_section = f"\n## Source\n\n- Format: {source_format}\n" if source_format else ""
    return prefix + content + ("" if content.endswith("\n") else "\n") + source_section + "\n" + _source_footer(document) + "\n"
