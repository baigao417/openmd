from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import openmd.cli as cli_module
from openmd.backends.base import BackendExtractRequest
from openmd.backends.xiaohongshu import OpenCliXiaohongshuNoteProvider
from openmd.cli import main as cli_main
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult
from openmd.core.package_validator import PackageValidationError, validate_package
from openmd.core.package_contract import XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION
from openmd.core.package_writer import write_item_package
from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry, classify_failure


class PackageFixtureValidationTests(unittest.TestCase):
    def test_v1_package_fixtures_validate(self) -> None:
        fixture_root = Path("docs/fixtures/openmd-package-v1")
        package_paths = sorted(fixture_root.glob("*.openmd"))
        self.assertGreaterEqual(len(package_paths), 4)

        for package_path in package_paths:
            with self.subTest(package=package_path.name):
                result = validate_package(package_path)
                self.assertTrue(result.ok, result.errors)

    def test_validator_rejects_profile_mismatched_sidecar(self) -> None:
        fixture = Path("docs/fixtures/openmd-package-v1/minimal-article.openmd")
        with tempfile.TemporaryDirectory() as tmp_dir:
            package_path = Path(tmp_dir) / "article-with-media-sidecar.openmd"
            shutil.copytree(fixture, package_path)
            (package_path / "timeline.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )

            result = validate_package(package_path)

            self.assertFalse(result.ok)
            self.assertIn("timeline.json is not allowed for profile: article", result.errors)

    def test_validator_rejects_bad_sidecar_shape_and_unknown_source_map_reference(self) -> None:
        fixture = Path("docs/fixtures/openmd-package-v1/minimal-media.openmd")
        with tempfile.TemporaryDirectory() as tmp_dir:
            package_path = Path(tmp_dir) / "bad-media-sidecar.openmd"
            shutil.copytree(fixture, package_path)
            (package_path / "timeline.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "t_0001",
                                "start": 0.0,
                                "end": 1.0,
                                "text": "bad source",
                                "source_map": [
                                    {
                                        "target": "transcript.md#bad",
                                        "source_ref": {"source_id": "missing_src"},
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = validate_package(package_path)

            self.assertFalse(result.ok)
            self.assertIn(
                "timeline.json[0].source_map[0].source_ref.source_id must refer to sources[].source_id",
                result.errors,
            )

    def test_validator_rejects_raw_or_credential_like_assets(self) -> None:
        fixture = Path("docs/fixtures/openmd-package-v1/minimal-article.openmd")
        cases = {
            "assets/raw.mp4": b"raw-media",
            "assets/raw.html": b"<html>cookie=secret token=secret</html>",
            "assets/session.json": b'{"session":"secret"}',
        }
        for relative_path, payload in cases.items():
            with self.subTest(relative_path=relative_path):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    package_path = Path(tmp_dir) / "bad-assets.openmd"
                    shutil.copytree(fixture, package_path)
                    asset_path = package_path / relative_path
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    asset_path.write_bytes(payload)

                    result = validate_package(package_path)

                    self.assertFalse(result.ok)

    def test_validator_rejects_generic_secret_text(self) -> None:
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post",
            id="post-1",
            title="Post 1",
            source_type="article",
        )
        secret_texts = [
            "cookie=secret-value should not persist",
            "token=secret-value should not persist",
            'headers={"cookie":"secret"} should not persist',
            "api response: secret raw body should not persist",
        ]
        for text in secret_texts:
            with self.subTest(text=text):
                result = TranscriptResult(
                    text=text,
                    status="verified",
                    source_level="native_text",
                    backend="mock",
                    title="Post 1",
                )

                with tempfile.TemporaryDirectory() as tmp_dir:
                    with self.assertRaises(PackageValidationError):
                        write_item_package(
                            item,
                            result,
                            tmp_dir,
                            captured_at="2026-06-18T00:00:00+00:00",
                        )


class PackageWriterTests(unittest.TestCase):
    def test_writer_persists_and_validates_component_contract(self) -> None:
        item = ContentItem(
            platform="example",
            input_type="single",
            url="https://example.com/post/1",
            id="post-1",
            source_type="mixed",
            raw_meta={
                "extraction_contract_version": "2",
                "required_components": ["native", "image-1"],
            },
        )
        result = TranscriptResult(
            text="native body\n\nimage text",
            status="partial",
            source_level="native_text",
            backend="mock",
            extraction_contract_version="2",
            components=[
                ExtractionComponent("native", "native_text", "verified", "native_text", "mock"),
                ExtractionComponent(
                    "image-1",
                    "image_ocr",
                    "unavailable",
                    "ocr",
                    "mock.ocr",
                    terminal_reason="image was not accessible",
                    error_code="image_unavailable",
                    failure_class="source_unavailable",
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            written = write_item_package(item, result, tmp_dir)
            metadata = json.loads(
                (written.package_path / "metadata.json").read_text(encoding="utf-8")
            )
            runlog = cli_module.build_item_runlog_entry(item, result)

        self.assertEqual(metadata["extraction_contract_version"], "2")
        self.assertEqual(
            [component["id"] for component in metadata["components"]],
            ["native", "image-1"],
        )
        self.assertEqual(runlog["terminal_gap_count"], 1)
        self.assertEqual(runlog["components"][1]["status"], "unavailable")

    def test_validator_requires_structured_unavailable_reason(self) -> None:
        item = ContentItem(
            platform="example",
            input_type="single",
            url="https://example.com/post/terminal",
            id="terminal",
            source_type="article",
        )
        result = TranscriptResult(
            text="native body",
            status="partial",
            source_level="native_text",
            backend="mock",
            components=[
                ExtractionComponent(
                    "content",
                    "text",
                    "unavailable",
                    "native_text",
                    "mock",
                    terminal_reason="source removed",
                    error_code="source_removed",
                    failure_class="source_unavailable",
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            written = write_item_package(item, result, tmp_dir)
            metadata_path = written.package_path / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["components"][0]["terminal_reason"] = None
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            validation = validate_package(written.package_path)

        self.assertFalse(validation.ok)
        self.assertIn(
            "metadata.json components[0].terminal_reason is required for unavailable components",
            validation.errors,
        )

    def test_component_warnings_are_recursively_sanitized_in_runlog(self) -> None:
        item = ContentItem(
            platform="example",
            input_type="single",
            url="https://example.com/post/1?xsec_token=input-secret&view=1",
            source_type="article",
        )
        result = TranscriptResult(
            text="safe text",
            status="verified",
            source_level="native_text",
            backend="mock",
            components=[
                ExtractionComponent(
                    "content",
                    "text",
                    "verified",
                    "native_text",
                    "mock",
                    warnings=[
                        "Authorization: Bearer auth-secret cookie=session-secret token=value-secret "
                        "https://example.com/media?xsec_token=url-secret&view=1"
                    ],
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "runlog.jsonl"
            append_runlog_entry(path, build_item_runlog_entry(item, result))
            content = path.read_text(encoding="utf-8").lower()

        for forbidden in (
            "auth-secret",
            "session-secret",
            "value-secret",
            "url-secret",
            "authorization",
            "bearer",
            "cookie",
            "xsec_token",
        ):
            self.assertNotIn(forbidden, content)
        self.assertIn("view=1", content)

    def test_legacy_package_is_not_skipped_for_new_required_contract(self) -> None:
        legacy_item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post/legacy",
            id="legacy",
            source_type="article",
        )
        upgraded_item = ContentItem(
            platform="web_article",
            input_type="single",
            url=legacy_item.url,
            id=legacy_item.id,
            source_type="article",
            raw_meta={
                "extraction_contract_version": "2",
                "required_components": ["native", "ocr"],
            },
        )
        legacy_result = TranscriptResult(
            text="native only",
            status="verified",
            source_level="native_text",
            backend="mock",
        )
        upgraded_result = TranscriptResult(
            text="native and OCR",
            status="verified",
            source_level="native_text",
            backend="mock",
            extraction_contract_version="2",
            components=[
                ExtractionComponent("native", "native_text", "verified", "native_text", "mock"),
                ExtractionComponent("ocr", "image_ocr", "verified", "ocr", "mock.ocr"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(legacy_item, legacy_result, tmp_dir)
            metadata_path = first.package_path / "metadata.json"
            legacy_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy_metadata.pop("extraction_contract_version")
            legacy_metadata.pop("components")
            metadata_path.write_text(json.dumps(legacy_metadata), encoding="utf-8")

            rewritten = write_item_package(upgraded_item, upgraded_result, tmp_dir)
            metadata = json.loads(
                (rewritten.package_path / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertFalse(rewritten.skipped_existing)
        self.assertEqual(rewritten.operation, "upgraded_contract")
        self.assertEqual(rewritten.package_path, first.package_path)
        self.assertEqual(metadata["extraction_contract_version"], "2")
        self.assertEqual({component["id"] for component in metadata["components"]}, {"native", "ocr"})

    def test_not_applicable_component_is_skippable_without_terminal_gap(self) -> None:
        item = ContentItem(
            platform="example",
            input_type="single",
            url="https://example.com/post/not-applicable",
            id="not-applicable",
            source_type="article",
            raw_meta={
                "extraction_contract_version": "2",
                "required_components": ["native", "image"],
            },
        )
        result = TranscriptResult(
            text="native body",
            status="verified",
            source_level="native_text",
            backend="mock",
            extraction_contract_version="2",
            components=[
                ExtractionComponent("native", "native_text", "verified", "native_text", "mock"),
                ExtractionComponent(
                    "image", "image_ocr", "not_applicable", "ocr", "mock.ocr"
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(item, result, tmp_dir)
            skipped = write_item_package(item, result, tmp_dir)
            runlog = build_item_runlog_entry(item, result)
            validation = validate_package(first.package_path)

        self.assertTrue(validation.ok, validation.errors)
        self.assertTrue(skipped.skipped_existing)
        self.assertEqual(runlog["terminal_gap_count"], 0)

    def test_contract_upgrade_failure_preserves_legacy_package(self) -> None:
        legacy_item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post/preserve",
            id="preserve",
            source_type="article",
        )
        required_item = ContentItem(
            platform=legacy_item.platform,
            input_type=legacy_item.input_type,
            url=legacy_item.url,
            id=legacy_item.id,
            source_type=legacy_item.source_type,
            raw_meta={
                "extraction_contract_version": "2",
                "required_components": ["native", "ocr"],
            },
        )
        legacy_result = TranscriptResult(
            text="legacy body",
            status="verified",
            source_level="native_text",
            backend="mock",
        )
        incomplete_result = TranscriptResult(
            text="new native only",
            status="partial",
            source_level="native_text",
            backend="mock",
            extraction_contract_version="2",
            components=[
                ExtractionComponent("native", "native_text", "verified", "native_text", "mock")
            ],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(legacy_item, legacy_result, tmp_dir)
            before = (first.package_path / "index.md").read_text(encoding="utf-8")
            with self.assertRaises(PackageValidationError):
                write_item_package(required_item, incomplete_result, tmp_dir)
            after = (first.package_path / "index.md").read_text(encoding="utf-8")

        self.assertEqual(after, before)
        self.assertIn("legacy body", after)

    def test_explicit_legacy_xiaohongshu_package_is_upgraded_by_default_composite(self) -> None:
        signed_key = "xsec" + "_token"
        note_id = "689c37a5000000001c0361a6"
        url = (
            f"https://www.xiaohongshu.com/discovery/item/{note_id}?"
            f"{signed_key}=synthetic-share"
        )
        canonical_url = f"https://www.xiaohongshu.com/discovery/item/{note_id}"
        legacy_item = ContentItem(
            platform="xiaohongshu",
            input_type="single",
            url=url,
            id=note_id,
            source_type="mixed",
        )

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return (
                0,
                json.dumps(
                    {
                        "data": {
                            "note_id": note_id,
                            "title": "Contract note",
                            "desc": "Native provider body.",
                        }
                    }
                ),
                "",
            )

        provider = OpenCliXiaohongshuNoteProvider(runner=fake_runner)
        result = provider.extract(
            BackendExtractRequest(
                item=legacy_item,
                backend_chain=(provider.name,),
                allow_fallback=False,
            )
        )
        self.assertTrue(result.has_usable_text, result.error_summary)

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(legacy_item, result, tmp_dir)
            metadata = json.loads(
                (first.package_path / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["extraction_contract_version"], "1")
            self.assertEqual([component["id"] for component in metadata["components"]], ["content"])

            composite_result = TranscriptResult(
                text=(
                    "## Native Note Text\n\nUpgraded body.\n\n"
                    "## Image OCR\n\n_Not applicable._\n\n"
                    "## Video Transcript\n\n_Not applicable._\n"
                ),
                status="verified",
                source_level="native_text",
                backend="xiaohongshu.composite",
                title="Contract note",
                extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
                components=[
                    ExtractionComponent(
                        "native_text", "native_text", "verified", "native_text", "xiaohongshu.composite",
                        text_ref="index.md#native-note-text",
                    ),
                    ExtractionComponent(
                        "image_ocr", "image_ocr", "not_applicable", "partial_extraction", "xiaohongshu.composite",
                        text_ref="index.md#image-ocr",
                    ),
                    ExtractionComponent(
                        "video_transcript", "video_transcript", "not_applicable", "partial_extraction", "xiaohongshu.composite",
                        text_ref="index.md#video-transcript",
                    ),
                ],
            )
            args = ["ingest", canonical_url, "--out", tmp_dir, "--package", "--enable-browser-backends"]
            with mock.patch.object(
                cli_module.BackendExecutor,
                "extract",
                return_value=composite_result,
            ) as extract_mock:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli_main(args), 0)
            extract_mock.assert_called_once()

            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            upgraded_metadata = json.loads(
                (first.package_path / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual([row["package_operation"] for row in runlog_rows], ["upgraded_contract"])
        self.assertEqual(upgraded_metadata["extraction_contract_version"], XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION)
        self.assertEqual(
            [component["id"] for component in upgraded_metadata["components"]],
            ["native_text", "image_ocr", "video_transcript"],
        )

    def test_writer_creates_valid_media_package(self) -> None:
        item = ContentItem(
            platform="xiaoyuzhou",
            input_type="single",
            url="https://www.xiaoyuzhoufm.com/episode/ep1?token=secret&view=1",
            id="ep1",
            title="Episode 1",
            source_type="audio",
            raw_meta={"permission_scope": "user_accessible", "language": "zh-CN"},
        )
        result = TranscriptResult(
            text="[00:00:00] hello package",
            status="verified",
            source_level="platform_transcript",
            backend="mock",
            title="Episode 1",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            written = write_item_package(
                item,
                result,
                tmp_dir,
                captured_at="2026-06-18T00:00:00+00:00",
            )

            self.assertEqual(written.operation, "created")
            self.assertTrue(written.package_path.name.endswith(".openmd"))
            self.assertTrue((written.package_path / "index.md").exists())
            self.assertTrue((written.package_path / "transcript.md").exists())
            validation = validate_package(written.package_path)
            self.assertTrue(validation.ok, validation.errors)

            package_text = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in written.package_path.rglob("*")
                if path.is_file() and path.suffix in {".md", ".json"}
            ).lower()
            self.assertNotIn("token=secret", package_text)
            self.assertIn("view=1", package_text)

    def test_writer_source_map_preserves_tier1_locators_when_available(self) -> None:
        cases = [
            (
                ContentItem(
                    platform="youtube",
                    input_type="single",
                    url="https://www.youtube.com/watch?v=yt123&token=secret&view=1",
                    id="yt123",
                    title="Video",
                    source_type="video",
                    raw_meta={"permission_scope": "user_accessible", "section_id": "intro"},
                ),
                TranscriptResult(
                    text="[00:00:12] hello",
                    status="verified",
                    source_level="native_caption",
                    backend="mock",
                    title="Video",
                    metadata={"video_id": "yt123", "timestamp": "00:00:12"},
                ),
                {"video_id": "yt123", "timestamp": "00:00:12", "section_id": "intro"},
            ),
            (
                ContentItem(
                    platform="qq_group",
                    input_type="collection",
                    url="qq://group/example",
                    id="conv1",
                    title="Conversation",
                    source_type="conversation",
                    raw_meta={"permission_scope": "user_accessible", "message_id": "msg-001"},
                ),
                TranscriptResult(
                    text="[2026-06-18] hi",
                    status="verified",
                    source_level="manual_export",
                    backend="mock",
                    title="Conversation",
                ),
                {"message_id": "msg-001"},
            ),
            (
                ContentItem(
                    platform="pdf",
                    input_type="single",
                    url="file:///tmp/source.pdf",
                    id="doc1",
                    title="Document",
                    source_type="document",
                    raw_meta={"permission_scope": "local_file", "page": 3},
                ),
                TranscriptResult(
                    text="page text",
                    status="verified",
                    source_level="third_party_extractor",
                    backend="mock",
                    title="Document",
                ),
                {"page": 3},
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            for item, result, expected in cases:
                with self.subTest(platform=item.platform):
                    written = write_item_package(
                        item,
                        result,
                        tmp_dir,
                        captured_at="2026-06-18T00:00:00+00:00",
                    )
                    validation = validate_package(written.package_path)
                    self.assertTrue(validation.ok, validation.errors)
                    sources = json.loads((written.package_path / "sources.json").read_text(encoding="utf-8"))
                    source_ref = sources["sources"][0]["source_map"][0]["source_ref"]
                    for key, value in expected.items():
                        self.assertEqual(source_ref[key], value)
                    self.assertNotIn("token=secret", json.dumps(source_ref).lower())
                    self.assertIn("source_id", source_ref)

    def test_writer_defaults_to_skip_existing_and_can_update_or_new_version(self) -> None:
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post/1",
            id="post-1",
            title="Post 1",
            source_type="article",
        )
        initial = TranscriptResult(
            text="initial body",
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Post 1",
        )
        updated = TranscriptResult(
            text="updated body",
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Post 1",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(
                item,
                initial,
                tmp_dir,
                captured_at="2026-06-18T00:00:00+00:00",
            )
            marker = first.package_path / "assets" / "marker.txt"
            marker.write_text("kept", encoding="utf-8")

            skipped = write_item_package(
                item,
                updated,
                tmp_dir,
                captured_at="2026-06-18T00:01:00+00:00",
            )
            self.assertTrue(skipped.skipped_existing)
            self.assertEqual(skipped.package_path, first.package_path)
            self.assertEqual(marker.read_text(encoding="utf-8"), "kept")
            self.assertIn("initial body", (first.package_path / "index.md").read_text(encoding="utf-8"))

            in_place = write_item_package(
                item,
                updated,
                tmp_dir,
                update=True,
                captured_at="2026-06-18T00:02:00+00:00",
            )
            self.assertEqual(in_place.package_path, first.package_path)
            self.assertIn("updated body", (first.package_path / "index.md").read_text(encoding="utf-8"))

            snapshot = write_item_package(
                item,
                updated,
                tmp_dir,
                new_version=True,
                captured_at="2026-06-18T00:03:00+00:00",
            )
            self.assertNotEqual(snapshot.package_path, first.package_path)
            self.assertIn("__v20260618T000300Z", snapshot.package_path.name)
            self.assertTrue(validate_package(snapshot.package_path).ok)

    def test_writer_does_not_publish_invalid_secret_package(self) -> None:
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post",
            id="post-1",
            title="Post 1",
            source_type="article",
        )
        result = TranscriptResult(
            text="Authorization: Bearer secret-value",
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Post 1",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(PackageValidationError):
                write_item_package(
                    item,
                    result,
                    tmp_dir,
                    captured_at="2026-06-18T00:00:00+00:00",
                )

            self.assertEqual(list(Path(tmp_dir).rglob("*.openmd")), [])

    def test_writer_invalid_update_preserves_existing_package(self) -> None:
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post/1",
            id="post-1",
            title="Post 1",
            source_type="article",
        )
        initial = TranscriptResult(
            text="initial body",
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Post 1",
        )
        invalid = TranscriptResult(
            text="Authorization: Bearer secret-value",
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Post 1",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_package(
                item,
                initial,
                tmp_dir,
                captured_at="2026-06-18T00:00:00+00:00",
            )

            with self.assertRaises(PackageValidationError):
                write_item_package(
                    item,
                    invalid,
                    tmp_dir,
                    update=True,
                    captured_at="2026-06-18T00:01:00+00:00",
                )

            index_text = (first.package_path / "index.md").read_text(encoding="utf-8")
            self.assertIn("initial body", index_text)
            self.assertNotIn("Authorization", index_text)
            self.assertTrue(validate_package(first.package_path).ok)

    def test_writer_escapes_frontmatter_control_lines_from_title(self) -> None:
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/post/1",
            id="post-1",
            title="Bad\nstatus: failed\n---\n# Injected",
            source_type="article",
        )
        result = TranscriptResult(
            text="body",
            status="verified",
            source_level="native_text",
            backend="mock",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            written = write_item_package(
                item,
                result,
                tmp_dir,
                captured_at="2026-06-18T00:00:00+00:00",
            )
            index_text = (written.package_path / "index.md").read_text(encoding="utf-8")
            frontmatter = index_text.removeprefix("---\n").split("\n---\n", 1)[0]

            self.assertNotIn("\n---\n# Injected", frontmatter)
            self.assertIn('title: "Bad status: failed --- # Injected"', frontmatter)
            self.assertTrue(validate_package(written.package_path).ok)


class PackageCliTests(unittest.TestCase):
    def test_doctor_reports_safe_session_boundary_without_private_state(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(["doctor"])

        payload = json.loads(stdout.getvalue())

        # Readiness depends on optional local tools; the contract under test is
        # the boundary report, and exit code must agree with "ok".
        self.assertEqual(exit_code, 0 if payload["ok"] else 1)
        self.assertEqual(payload["command"], "doctor")
        self.assertEqual(payload["official_command"], "openmd")
        self.assertFalse(payload["safe_session_boundary"]["core_owns_platform_sessions"])
        self.assertFalse(payload["safe_session_boundary"]["reads_private_browser_profiles"])
        self.assertIn("auth_required", payload["structured_states"])
        self.assertIn("sidecar_unreachable", payload["structured_states"])

    def test_ingest_package_smoke_uses_mock_backend_and_sanitizes_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        "https://example.com/article?token=secret&view=1",
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_path = Path(payload["outputs"][0])
            runlog_path = Path(tmp_dir) / "runlog.jsonl"

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(package_path.exists())
            self.assertEqual(package_path.suffix, ".openmd")
            self.assertTrue(validate_package(package_path).ok)
            sources = json.loads((package_path / "sources.json").read_text(encoding="utf-8"))
            source_map = sources["sources"][0]["source_map"]
            self.assertEqual(source_map[0]["target"], "index.md#content")
            self.assertEqual(source_map[0]["source_ref"]["source_id"], "src_001")
            self.assertNotIn("token=secret", source_map[0]["source_ref"]["url"].lower())
            self.assertIn("view=1", source_map[0]["source_ref"]["url"].lower())
            combined = (
                (package_path / "metadata.json").read_text(encoding="utf-8")
                + (package_path / "sources.json").read_text(encoding="utf-8")
                + (package_path / "index.md").read_text(encoding="utf-8")
                + runlog_path.read_text(encoding="utf-8")
            ).lower()
            self.assertNotIn("token=secret", combined)
            self.assertIn("view=1", combined)

    def test_package_runlog_records_created_and_skipped_existing_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = [
                "ingest",
                "https://example.com/article?token=secret&view=1",
                "--out",
                tmp_dir,
                "--backend",
                "mock",
                "--package",
            ]
            for _ in range(2):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(args)
                self.assertEqual(exit_code, 0)

            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(
                [row["package_operation"] for row in runlog_rows],
                ["created", "skipped_existing"],
            )
            self.assertEqual(runlog_rows[0]["package_id"], runlog_rows[1]["package_id"])
            self.assertNotIn("token=secret", json.dumps(runlog_rows).lower())
            self.assertIn("view=1", json.dumps(runlog_rows).lower())

    def test_existing_package_skip_does_not_call_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = [
                "ingest",
                "https://example.com/article?token=secret&view=1",
                "--out",
                tmp_dir,
                "--backend",
                "mock",
                "--package",
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli_main(args), 0)

            with mock.patch.object(
                cli_module.BackendExecutor,
                "extract",
                side_effect=AssertionError("extract should not run for an existing package"),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(args)

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(runlog_rows[-1]["package_operation"], "skipped_existing")

    def test_cli_package_success_is_validator_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(
                cli_module,
                "validate_package_or_raise",
                side_effect=PackageValidationError("bad package"),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = cli_main(
                        [
                            "ingest",
                            "https://example.com/article",
                            "--out",
                            tmp_dir,
                            "--backend",
                            "mock",
                            "--package",
                        ]
                    )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(runlog_rows[0]["status"], "failed")
        self.assertEqual(runlog_rows[0]["error_code"], "package_validation_failed")
        self.assertIn("bad package", runlog_rows[0]["error_summary"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(list(Path(tmp_dir).rglob("*.openmd")), [])

    def test_new_version_conflicts_with_update_or_overwrite(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = cli_main(
                [
                    "ingest",
                    "https://example.com/article",
                    "--out",
                    "out",
                    "--backend",
                    "mock",
                    "--package",
                    "--new-version",
                    "--update",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot be combined", stderr.getvalue())

    def test_batch_package_root_writes_packages_under_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            urls_file = Path(tmp_dir) / "urls.txt"
            urls_file.write_text(
                "https://example.com/a\nhttps://example.com/b\n",
                encoding="utf-8",
            )
            package_root = Path(tmp_dir) / "library"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "batch",
                        str(urls_file),
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--package",
                        "--package-root",
                        str(package_root),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_paths = [Path(value) for value in payload["outputs"]]

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["success_count"], 2)
            self.assertEqual(len(package_paths), 2)
            for package_path in package_paths:
                self.assertTrue(package_path.is_relative_to(package_root))
                self.assertTrue(validate_package(package_path).ok)

    def test_batch_guardrail_stops_after_consecutive_failures_with_resume_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            urls_file = Path(tmp_dir) / "urls.txt"
            urls_file.write_text(
                "https://example.com/a\nhttps://example.com/b\nhttps://example.com/c\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "batch",
                        str(urls_file),
                        "--out",
                        tmp_dir,
                        "--backend",
                        "missing.backend",
                        "--guardrail-failure-streak",
                        "2",
                        "--guardrail-item-threshold",
                        "2",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["item_count"], 2)
            self.assertEqual(payload["failed_count"], 2)
            self.assertIn("consecutive_failure_stop", payload["guardrail_signals"])
            self.assertEqual(runlog_rows[0]["event_type"], "guardrail")
            self.assertEqual(runlog_rows[0]["guardrail_signals"], ["large_item_count"])
            self.assertEqual(runlog_rows[-1]["event_type"], "guardrail")
            self.assertEqual(runlog_rows[-1]["error_code"], "guardrail_consecutive_failures")
            self.assertEqual(runlog_rows[-1]["failure_class"], "runtime_transient")
            self.assertIn("item 3 of 3", runlog_rows[-1]["resume_pointer"])


class CliEntryScriptTests(unittest.TestCase):
    def test_repo_local_openmd_wrapper_invokes_module_entry(self) -> None:
        script = Path("scripts/openmd.ps1").read_text(encoding="utf-8")

        self.assertIn("python", script.lower())
        self.assertIn("-m openmd", script)
        self.assertIn("PYTHONPATH", script)

    def test_omd_wrapper_delegates_to_openmd_wrapper(self) -> None:
        script = Path("scripts/omd.ps1").read_text(encoding="utf-8")

        self.assertIn("openmd.ps1", script)


class RunlogFailureClassificationTests(unittest.TestCase):
    def test_goal_c_session_cache_and_local_state_codes_are_classified(self) -> None:
        cases = {
            "auth_required": "user_resolvable",
            "session_expired": "user_resolvable",
            "permission_required": "user_resolvable",
            "cache_stale": "user_resolvable",
            "refresh_needed": "user_resolvable",
            "export_required": "user_resolvable",
            "sidecar_unreachable": "runtime_transient",
            "no_delta": "source_unavailable",
            "no_data": "source_unavailable",
        }

        for error_code, expected in cases.items():
            with self.subTest(error_code=error_code):
                self.assertEqual(classify_failure(error_code), expected)


class FragmentSanitizationTests(unittest.TestCase):
    def test_sanitize_url_strips_fragment_secrets(self) -> None:
        from openmd.core.secrets import sanitize_url
        # Fragment-only URL: must strip secret fragment params
        result = sanitize_url("https://example.com/article#session=SECRET&view=1")
        self.assertNotIn("SECRET", result)
        self.assertNotIn("session=", result)

        # URL with query + secret fragment: strip fragment secret, keep safe query
        result = sanitize_url("https://example.com/article?view=1#access_token=SECRET")
        self.assertNotIn("SECRET", result)
        self.assertNotIn("access_token", result)
        self.assertIn("view=1", result)

        # Fragment with mix of safe and sensitive keys
        result = sanitize_url("https://example.com/#state=SAFE&token=SECRET")
        self.assertNotIn("SECRET", result)
        self.assertNotIn("token=", result)
        self.assertIn("state=SAFE", result)

        # Non-kv fragment must be preserved (not key=value format)
        result = sanitize_url("https://example.com/article#section-title")
        self.assertIn("section-title", result)

        # URL with no query and no fragment: no change
        result = sanitize_url("https://example.com/article")
        self.assertEqual(result, "https://example.com/article")

    def test_package_validator_allows_legitimate_token_cookie_prose(self) -> None:
        from openmd.core.models import ContentItem, TranscriptResult
        from openmd.core.package_writer import write_item_package
        from openmd.core.package_validator import validate_package

        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/concepts",
            id="concepts-1",
            title="Web Concepts",
            source_type="article",
        )
        # This text discusses tokens/cookies/headers as concepts — must NOT be rejected
        legitimate_text = (
            "This article explains token budgets, HTTP cookie behavior, "
            "and header parsing as concepts. Understanding how auth "
            "flows work helps developers build better systems. No raw "
            "stdout/stderr, raw subtitle rows, media, or browser credential "
            "artifacts were persisted."
        )
        result = TranscriptResult(
            text=legitimate_text,
            status="verified",
            source_level="native_text",
            backend="mock",
            title="Web Concepts",
        )

        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            written = write_item_package(
                item,
                result,
                tmp_dir,
                captured_at="2026-06-23T00:00:00+00:00",
            )
            validation = validate_package(written.package_path)
            self.assertTrue(validation.ok, validation.errors)


if __name__ == "__main__":
    unittest.main()
