"""Offline regressions for readable output; source words and order stay intact."""

import re
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

    def test_video_continuation_and_article_table_have_distinct_rendering(self):
        text = "[00:00:00] caption\nUnknown source line"
        self.assertEqual(format_transcript(text, source_type="video", platform="youtube"),
                         "**00:00:00**\n\ncaption Unknown source line")
        article = self.table([("0:00", "A", "article")])
        self.assertEqual(format_transcript(article, source_type="article", platform="youtube"), article)

    def test_raw_video_body_remains_exact(self):
        text = self.table([("0:00", "A", "first"), ("1:00", "A", "second")]) + "  \n\n"
        document = MarkdownDocument({"platform": "youtube", "source_type": "video"}, "Video", text)
        self.assertIn("\n\n" + text + "\n", render_document(document, raw=True))
        self.assertNotIn("| timestamp |", render_document(document))

    def test_untimed_continuation_and_leading_heading_are_recognized(self):
        text = "# Episode title\n[00:00:00] First caption\ncontinues here\n[00:00:40] Second caption"
        rendered = format_transcript(text, source_type="audio", platform="xiaoyuzhou")
        self.assertTrue(rendered.startswith("# Episode title\n\n**00:00:00**"))
        self.assertIn("First caption continues here", rendered)
        self.assertIn("\n\n**00:00:40**\n\nSecond caption", rendered)

    def test_xiaoe_transcript_section_uses_shared_caption_layout(self):
        text = "# Lesson\n\n## Transcript\n\n[00:00] First source line.\n[01:00] Second source line."
        expected = "# Lesson\n\n## Transcript\n\n**00:00**\n\nFirst source line.\n\n**01:00**\n\nSecond source line."
        self.assertEqual(format_transcript(text, source_type="video", platform="xiaoe"), expected)
        document = MarkdownDocument({"platform": "xiaoe", "source_type": "video"}, "Lesson", text)
        self.assertIn("## Transcript\n\n**00:00**", render_document(document))
        self.assertIn(text, render_document(document, raw=True))

    def test_xiaoe_package_index_and_sidecar_share_readable_layout(self):
        import tempfile
        from openmd.core.package_writer import write_item_package

        for source_type in ("audio", "video"):
            for prefix in ("", "## Transcript\n\n", "# Lesson\n\n## Transcript\n\n"):
                with self.subTest(source_type=source_type, prefix=prefix), tempfile.TemporaryDirectory() as directory:
                    item = ContentItem(platform="xiaoe", input_type="single", source_type=source_type,
                                       url="https://example.test/course/lesson")
                    result = TranscriptResult(text=prefix + "[00:00] First source line.\n[01:00] Second source line.",
                                              title="Lesson", status="verified", source_level="media_asr", backend="fixture")
                    package = write_item_package(item, result, directory).package_path
                    expected = "**00:00**\n\nFirst source line.\n\n**01:00**\n\nSecond source line."
                    for name in ("index.md", "transcript.md"):
                        rendered = (package / name).read_text(encoding="utf-8")
                        self.assertIn(expected, rendered)
                        self.assertNotIn("[00:00]", rendered)
                        self.assertEqual(rendered.count("\n## Transcript\n"), 1)
                        self.assertEqual(rendered.count("# Lesson\n"), 1)

    def test_seconds_based_index_table_is_grouped(self):
        text = (
            "| index | start | end | text |\n| --- | --- | --- | --- |\n"
            "| 1 | 0.00s | 4.00s | first line |\n| 2 | 4.00s | 8.00s | second line |"
        )
        rendered = format_transcript(text, source_type="video", platform="youtube")
        self.assertEqual(rendered, "**00:00:00**\n\nfirst line second line")

    def test_unknown_long_unbroken_transcript_gets_one_paragraph_per_line(self):
        lines = [f"synthetic source line {index}" for index in range(20)]
        text = "\n".join(lines)
        rendered = format_transcript(text, source_type="audio", platform="xiaoyuzhou")
        self.assertEqual(rendered, "\n\n".join(lines))

    def test_long_podcast_captions_are_not_split_by_six_second_gaps(self):
        rows = [(f"0:{seconds:02}", "", "A long synthetic caption with more than forty characters.")
                for seconds in (0, 10, 20, 30)]
        rendered = format_transcript("\n".join(f"[{time}] {text}" for time, _, text in rows),
                                     source_type="audio", platform="xiaoyuzhou")
        self.assertEqual(rendered.count("**"), 2)
        self.assertEqual(rendered.count("A long synthetic caption"), 4)

    def test_formatting_preserves_source_characters_and_order(self):
        cases = (
            "# Synthetic title\n[00:00:00] First synthetic line\ncontinued source text\n[00:00:20] Final source line",
            "| index | start | end | text |\n| --- | --- | --- | --- |\n"
            "| 1 | 0.00s | 3.00s | first table phrase |\n| 2 | 3.00s | 6.00s | final table phrase |",
        )
        for text in cases:
            with self.subTest(text=text[:24]):
                rendered = format_transcript(text, source_type="audio", platform="fixture")
                if "| index |" in text:
                    source_parts = [line.split("|")[-2].strip() for line in text.splitlines()[2:]]
                else:
                    source_parts = []
                    for line in text.splitlines():
                        if line.startswith("# "):
                            source_parts.append(line[2:])
                        else:
                            source_parts.append(re.sub(r"^\[[^\]]+\]\s*", "", line))
                source_words = re.sub(r"\s+", "", "".join(source_parts))
                output = re.sub(r"\*\*[^*]+\*\*", "", rendered)
                output = re.sub(r"^#\s*", "", output, flags=re.M)
                output = re.sub(r"\s+", "", output)
                self.assertEqual(output, source_words)
