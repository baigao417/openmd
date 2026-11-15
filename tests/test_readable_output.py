"""Offline regressions for readable output; source words and order stay intact."""

import unittest

from openmd.core.models import ContentItem, MarkdownDocument, TranscriptResult
from openmd.core.readable import format_transcript
from openmd.core.renderer import render_document, render_markdown_document


class ReadableOutputTests(unittest.TestCase):
    def document(self, platform="x", *, title="First sentence.", text=None, **fields):
        return MarkdownDocument(
            frontmatter={"platform": platform, "source_type": "article", **fields},
            title=title,
            content=text if text is not None else title + "\n\nSecond sentence.",
        )

    def test_x_keeps_body_sentence_used_as_title(self):
        title = "Actual progress comes from hard-to-build technology."
        for first in (title, "# " + title):
            with self.subTest(first=first):
                document = self.document(title=title, text=first + "\n\nWhich comes next.")
                for raw in (False, True):
                    rendered = render_document(document, raw=raw)
                    self.assertIn("\n\n" + document.content + "\n", rendered)
                    self.assertEqual(rendered.count(title), 3)  # YAML, heading, body.

    def test_independent_article_title_is_deduplicated_only_in_readable_mode(self):
        for first in ("First sentence.", "# First sentence."):
            with self.subTest(first=first):
                document = self.document("web_article", text=first + "\n\nSecond sentence.")
                rendered = render_document(document)
                body = rendered.split("# First sentence.\n\n", 1)[1]
                self.assertTrue(body.startswith("Second sentence.\n"))
                self.assertIn("\n\n" + document.content + "\n", render_document(document, raw=True))

    def test_unrelated_first_line_is_preserved(self):
        document = self.document("web_article", title="Independent title", text="Opening sentence.\n\nBody.")
        self.assertIn(document.content, render_document(document))

    def test_douyin_image_author_text_preserves_title_body_and_authored_timestamps(self):
        for body in ("广西加油。#广西 #广西洪水", "[00:01] 重复\n[00:02] 重复  \n\n最后一段\n"):
            for source_type in ("image_text", "video"):
                with self.subTest(body=body, source_type=source_type):
                    item = ContentItem(platform="douyin", input_type="single", url="https://www.douyin.com/note/7123456789012345678", title=body, source_type=source_type)
                    result = TranscriptResult(
                        text=body, title=body, status="verified", source_level="native_text", backend="douyin.post-text",
                        metadata={"transcript_method": "image_post_text", "media_kind": "image_text", "content_kind": "author_text"},
                    )
                    document = render_markdown_document(item, result)
                    self.assertEqual(document.frontmatter["source_type"], "image_text")
                    for raw in (False, True):
                        rendered = render_document(document, raw=raw)
                        self.assertIn("\n\n" + body + ("" if body.endswith("\n") else "\n") + "\n*来源：作者原文（图文）。*", rendered)
                        self.assertNotIn("语音识别", rendered)
                        self.assertNotIn("识别错误保持原样", rendered)
                        self.assertNotIn("image_post_author_text:", rendered)

    def test_empty_frontmatter_values_are_omitted_in_both_modes(self):
        for empty in (None, "", "  \t\n"):
            with self.subTest(empty=empty):
                document = self.document(author=empty, url=empty, published=empty, published_at=empty)
                for raw in (False, True):
                    header = render_document(document, raw=raw).split("---", 2)[1]
                    self.assertEqual(header.strip().splitlines(), ['title: "First sentence."', 'platform: "x"'])

    def test_known_publication_date_and_source_fields_are_retained(self):
        document = self.document(
            author="Author", url="https://example.com/post", published_at="2026-11-07T10:00:00Z",
        )
        header = render_document(document).split("---", 2)[1]
        self.assertIn('published: "2026-11-07"', header)
        self.assertIn('author: "Author"', header)
        self.assertIn('url: "https://example.com/post"', header)

    def test_image_failure_footer_counts_raw_and_wrapped_notes(self):
        item = ContentItem(platform="weixin", input_type="single", url="https://example.com/post")
        for count in (0, 1, 2):
            with self.subTest(count=count):
                result = TranscriptResult(
                    text="Article body", status="verified", source_level="native_text", backend="fixture",
                    source_notes=[f"asset_download_failed: https://example.com/{i}.png (failure)" for i in range(count)],
                )
                wrapped = render_markdown_document(item, result)
                direct = MarkdownDocument(wrapped.frontmatter, wrapped.title, wrapped.content, result.source_notes)
                for document in (wrapped, direct):
                    for raw in (False, True):
                        rendered = render_document(document, raw=raw)
                        if count:
                            self.assertIn(f"{count} 张图片下载失败，保留远程链接", rendered.splitlines()[-1])
                        else:
                            self.assertNotIn("图片下载失败", rendered)
                        self.assertNotIn("asset_download_failed", rendered)


class CaptionGroupingTests(unittest.TestCase):
    def table(self, rows):
        return "| timestamp | speaker | text |\n| --- | --- | --- |\n" + "\n".join(
            f"| {timestamp} | {speaker} | {text} |" for timestamp, speaker, text in rows
        )

    def test_same_speaker_is_split_at_sixty_seconds_without_punctuation(self):
        for speaker in ("Speaker 2", ""):
            with self.subTest(speaker=speaker):
                rows = [(f"{seconds // 60}:{seconds % 60:02}", speaker, f"fragment{seconds}")
                        for seconds in (0, 29, 59, 60, 89, 119, 120, 179, 180)]
                text = self.table(rows)
                rendered = format_transcript(text, source_type="video", platform="youtube")
                blocks = rendered.split("\n\n")
                self.assertEqual(blocks[1::2], [
                    "fragment0 fragment29 fragment59", "fragment60 fragment89 fragment119",
                    "fragment120 fragment179", "fragment180",
                ])
                prefix = f"{speaker} (" if speaker else ""
                suffix = ")" if speaker else ""
                self.assertEqual(blocks[::2], [f"**{prefix}{time}{suffix}**" for time in ("0:00", "1:00", "2:00", "3:00")])

    def test_chapters_speaker_changes_and_reversed_times_remain_boundaries(self):
        text = self.table([
            ("0:00", "A", "first"), ("0:01", "B", "second"),
            ("0:02", "", "[Chapter] Next chapter"), ("0:03", "B", "third"),
            ("0:01", "B", "fourth"),
        ])
        self.assertEqual(format_transcript(text, source_type="video", platform="youtube"),
                         "**A (0:00)**\n\nfirst\n\n**B (0:01)**\n\nsecond\n\n"
                         "## Next chapter (0:02)\n\n**B (0:03)**\n\nthird\n\n**B (0:01)**\n\nfourth")

    def test_line_captions_keep_existing_thirty_second_window(self):
        text = "[00:00:00] first\n[00:00:05] second\n[00:00:30] third"
        self.assertEqual(format_transcript(text, source_type="video", platform="bilibili"),
                         "**00:00:00**\n\nfirst second\n\n**00:00:30**\n\nthird")

    def test_mixed_or_article_text_passes_through(self):
        for text, source_type in (("[00:00:00] caption\nUnknown source line", "video"),
                                  (self.table([("0:00", "A", "article")]), "article")):
            self.assertEqual(format_transcript(text, source_type=source_type, platform="youtube"), text)

    def test_raw_video_body_remains_exact(self):
        text = self.table([("0:00", "A", "first"), ("1:00", "A", "second")]) + "  \n\n"
        document = MarkdownDocument({"platform": "youtube", "source_type": "video"}, "Video", text)
        self.assertIn("\n\n" + text + "\n", render_document(document, raw=True))
        self.assertNotIn("| timestamp |", render_document(document))
