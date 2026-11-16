"""Xiaohongshu composite multimodal provider and adapter tests."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import openmd.cli as cli_module
from openmd.adapters.registry import create_default_registry
from openmd.routing.loader import default_backend_chains
from openmd.adapters.xiaohongshu import XiaohongshuAdapter
from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import BackendExecutor, BackendProviderRegistry, SkeletonBackendProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.backends.xiaohongshu import OPENCLI_XIAOHONGSHU_NOTE_BACKEND, OpenCliXiaohongshuNoteProvider
from openmd.backends.xiaohongshu_providers import (
    XIAOHONGSHU_COMPOSITE_BACKEND,
    XIAOHONGSHU_HARD_SUBTITLE_BACKEND,
    XiaohongshuCompositeProvider,
    build_opencli_xiaohongshu_note_assets_args,
)
from openmd.core.models import ContentItem
from openmd.core.package_contract import (
    XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
    package_id_for_item,
)
from openmd.core.package_writer import write_item_package
from openmd.core.renderer import render_document, render_markdown_document


NOTE_ID = "689c37a5000000001c0361a6"
PROFILE_ID = "683e522f000000001d008dba"
NOTE_URL = f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}"


class FakeAsr:
    name = "fake.asr"

    def __init__(self, *, text: str = "ASR transcript", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[Path] = []

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path: Path, *, timeout: int) -> str:
        self.calls.append(audio_path)
        if self.error:
            raise self.error
        return self.text


def _item(*, source_type: str = "mixed", media_kind: str = "mixed") -> ContentItem:
    return ContentItem(
        platform="xiaohongshu",
        input_type="single",
        url=NOTE_URL,
        id=NOTE_ID,
        title="Fixture note",
        author="Fixture author",
        source_type=source_type,
        raw_meta={
            "profile_id": PROFILE_ID,
            "media_kind": media_kind,
            "extraction_contract_version": XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
            "required_components": ["native_text", "image_ocr", "video_transcript"],
        },
    )


def _request(item: ContentItem) -> BackendExtractRequest:
    return BackendExtractRequest(
        item=item,
        backend_chain=(XIAOHONGSHU_COMPOSITE_BACKEND,),
        allow_fallback=False,
    )


def _plugin_rows(
    *,
    media_kind: str,
    image_count: int = 0,
    video_count: int = 0,
    native_text: str = "Creator-authored body.",
    native_transcript: str = "",
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "row_type": "note",
            "note_id": NOTE_ID,
            "title": "Fixture note",
            "author": "Fixture author",
            "native_text": native_text,
            "tags": ["#fixture"],
            "media_kind": media_kind,
            "native_transcript": native_transcript,
            "image_count": image_count,
            "video_count": video_count,
            "asset_index": 0,
            "asset_type": "",
            "asset_status": "metadata_ready",
            "local_path": "",
        }
    ]
    for index in range(1, image_count + 1):
        rows.append(
            {
                "row_type": "asset",
                "asset_index": index,
                "asset_type": "image",
                "asset_status": "success",
                "asset_error_code": "",
                "local_path": f"image_{index:03d}.jpg",
            }
        )
    if video_count:
        rows.append(
            {
                "row_type": "asset",
                "asset_index": image_count + 1,
                "asset_type": "video",
                "asset_status": "success",
                "asset_error_code": "",
                "local_path": "video_001.mp4",
            }
        )
    return rows


def _asset_runner(
    rows: list[dict[str, object]],
    *,
    temp_roots: list[Path],
    selectors: list[str | None] | None = None,
    commands: list[list[str]] | None = None,
):
    def run(args: list[str], timeout: int, selector: str | None) -> tuple[int, str, str]:
        if selectors is not None:
            selectors.append(selector)
        if commands is not None:
            commands.append(list(args))
        output = Path(args[args.index("--output") + 1])
        temp_roots.append(output)
        for row in rows:
            local = str(row.get("local_path") or "")
            if local:
                (output / local).write_bytes(b"fixture-media")
        serialized = json.dumps(rows)
        assert "xsec_token" not in serialized
        assert "xhscdn" not in serialized
        assert "runtime_note_url" not in serialized
        return 0, serialized, ""

    return run


class XiaohongshuAdapterTests(unittest.TestCase):
    def test_direct_note_declares_full_contract_and_canonicalizes_signed_input(self) -> None:
        signed = (
            f"https://www.xiaohongshu.com/user/profile/{PROFILE_ID}/{NOTE_ID}"
            "?xsec_token=runtime-secret&xsec_source=pc_user"
        )
        item = XiaohongshuAdapter().resolve_items(signed)[0]

        self.assertEqual(item.url, NOTE_URL)
        self.assertEqual(item.id, NOTE_ID)
        self.assertEqual(item.raw_meta["profile_id"], PROFILE_ID)
        self.assertEqual(item.raw_meta["extraction_contract_version"], XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION)
        self.assertEqual(item.raw_meta["required_components"], ["native_text", "image_ocr", "video_transcript"])
        self.assertIn("runtime-secret", item.raw_meta["runtime_note_url"])
        self.assertEqual(default_backend_chains(effective=False)["xiaohongshu"], (XIAOHONGSHU_COMPOSITE_BACKEND,))
        self.assertEqual(create_default_registry().backend_chain("xiaohongshu")[0], XIAOHONGSHU_COMPOSITE_BACKEND)

        mock_result = XiaohongshuAdapter().extract_text(item, backend="mock")
        self.assertEqual([component.id for component in mock_result.components], [
            "native_text", "image_ocr", "video_transcript",
        ])
        self.assertEqual([component.status for component in mock_result.components], [
            "verified", "not_applicable", "not_applicable",
        ])
        with tempfile.TemporaryDirectory() as tmp_dir:
            package = write_item_package(item, mock_result, tmp_dir)
            accepted_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in package.package_path.rglob("*")
                if path.is_file()
            )
        self.assertNotIn("runtime-secret", accepted_text)
        self.assertNotIn("xsec_token", accepted_text)

    def test_legacy_note_backend_remains_explicit(self) -> None:
        safe = create_runtime_backend_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(backend_override=OPENCLI_XIAOHONGSHU_NOTE_BACKEND),
        )
        self.assertIsInstance(safe.provider_for(OPENCLI_XIAOHONGSHU_NOTE_BACKEND), SkeletonBackendProvider)
        enabled = create_runtime_backend_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(
                backend_override=OPENCLI_XIAOHONGSHU_NOTE_BACKEND,
                enable_browser_backends=True,
            ),
        )
        self.assertIsInstance(enabled.provider_for(OPENCLI_XIAOHONGSHU_NOTE_BACKEND), OpenCliXiaohongshuNoteProvider)
        self.assertEqual(
            create_default_registry().backend_chain(
                "xiaohongshu",
                requested=OPENCLI_XIAOHONGSHU_NOTE_BACKEND,
            ),
            (OPENCLI_XIAOHONGSHU_NOTE_BACKEND,),
        )

        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, json.dumps({"note_id": NOTE_ID, "title": "Legacy", "content": "Legacy body"}), ""

        signed_item = XiaohongshuAdapter().resolve_items(
            f"https://www.xiaohongshu.com/user/profile/{PROFILE_ID}/{NOTE_ID}?xsec_token=legacy-runtime"
        )[0]
        result = OpenCliXiaohongshuNoteProvider(runner=fake_runner).extract(
            BackendExtractRequest(
                item=signed_item,
                backend_chain=(OPENCLI_XIAOHONGSHU_NOTE_BACKEND,),
                allow_fallback=False,
            )
        )
        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertIn("xsec_token=legacy-runtime", calls[0][3])
        self.assertNotIn("legacy-runtime", str(result.metadata))

    def test_xhslink_is_a_single_contracted_item_with_runtime_selector_only(self) -> None:
        short_url = "http://xhslink.com/o/example-share"
        item = XiaohongshuAdapter().resolve_items(short_url)[0]
        self.assertEqual(item.input_type, "single")
        self.assertEqual(item.url, short_url)
        self.assertEqual(item.raw_meta["runtime_selector"], short_url)
        self.assertTrue(item.raw_meta["identity_unresolved"])
        self.assertEqual(item.raw_meta["required_components"], ["native_text", "image_ocr", "video_transcript"])

    def test_executor_resolves_xhslink_identity_without_downloading_media(self) -> None:
        short_url = "http://xhslink.com/o/identity-share"
        item = XiaohongshuAdapter().resolve_items(short_url)[0]
        selectors: list[str | None] = []
        commands: list[list[str]] = []
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(
                _plugin_rows(media_kind="image_text", image_count=2),
                temp_roots=[],
                selectors=selectors,
                commands=commands,
            ),
        )
        registry = BackendProviderRegistry(allow_skeleton_fallback=False)
        registry.register(XIAOHONGSHU_COMPOSITE_BACKEND, provider)
        executor = BackendExecutor(provider_registry=registry)

        resolution = executor.resolve_item_identity(
            item,
            (XIAOHONGSHU_COMPOSITE_BACKEND,),
        )

        self.assertTrue(resolution.ok, resolution.error_summary)
        self.assertEqual(resolution.item.id, NOTE_ID)
        self.assertEqual(resolution.item.url, NOTE_URL)
        self.assertEqual(resolution.item.source_type, "image_text")
        self.assertEqual(
            resolution.item.raw_meta["required_components"],
            ["native_text", "image_ocr"],
        )
        self.assertFalse(resolution.item.raw_meta["identity_unresolved"])
        self.assertEqual(selectors, [short_url])
        argv = " ".join(commands[0])
        self.assertIn("--selector_stdin true", argv)
        self.assertIn("--download false", argv)
        self.assertNotIn(short_url, argv)
        canonical = XiaohongshuAdapter().resolve_items(NOTE_URL)[0]
        self.assertEqual(package_id_for_item(resolution.item), package_id_for_item(canonical))

    def test_xhslink_cli_reruns_resolve_identity_then_skip_without_extraction(self) -> None:
        adapter = XiaohongshuAdapter()
        canonical = adapter.resolve_items(NOTE_URL)[0]
        result = adapter.extract_text(canonical, backend="mock")
        selectors: list[str | None] = []
        commands: list[list[str]] = []
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(
                _plugin_rows(media_kind="text"),
                temp_roots=[],
                selectors=selectors,
                commands=commands,
            ),
        )
        registry = BackendProviderRegistry(allow_skeleton_fallback=False)
        registry.register(XIAOHONGSHU_COMPOSITE_BACKEND, provider)
        executor = BackendExecutor(provider_registry=registry)
        short_links = (
            "http://xhslink.com/o/identity-rerun-a",
            "http://xhslink.com/o/identity-rerun-b",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            package = write_item_package(canonical, result, tmp_dir)
            with mock.patch.object(
                cli_module,
                "_build_backend_executor",
                return_value=executor,
            ), mock.patch.object(
                provider,
                "extract",
                side_effect=AssertionError("skip-existing must prevent OCR/ASR extraction"),
            ):
                for short_link in short_links:
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        exit_code = cli_module.main(
                            [
                                "ingest",
                                short_link,
                                "--out",
                                tmp_dir,
                                "--package",
                                "--backend",
                                XIAOHONGSHU_COMPOSITE_BACKEND,
                                "--enable-browser-backends",
                            ]
                        )
                    self.assertEqual(exit_code, 0, stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(selectors, list(short_links))
        self.assertTrue(all("--download false" in " ".join(command) for command in commands))
        self.assertTrue(all(short not in " ".join(command) for short, command in zip(short_links, commands)))
        self.assertEqual(
            [row["package_operation"] for row in runlog_rows],
            ["skipped_existing", "skipped_existing"],
        )
        self.assertEqual(package.package_id, f"xiaohongshu_{NOTE_ID}")

    def test_xhslink_identity_failure_logs_and_never_uses_hashed_skip_or_extract(self) -> None:
        short_link = "http://xhslink.com/o/identity-unavailable"
        executor = BackendExecutor(
            provider_registry=BackendProviderRegistry(allow_skeleton_fallback=False)
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with mock.patch.object(
                cli_module,
                "_build_backend_executor",
                return_value=executor,
            ), mock.patch.object(
                cli_module,
                "_maybe_skip_existing_package",
                side_effect=AssertionError("unresolved identity must not enter skip-existing"),
            ), mock.patch.object(
                executor,
                "extract",
                side_effect=AssertionError("unresolved identity must not be extracted"),
            ), contextlib.redirect_stdout(stdout):
                exit_code = cli_module.main(
                    [
                        "ingest",
                        short_link,
                        "--out",
                        tmp_dir,
                        "--package",
                        "--backend",
                        XIAOHONGSHU_COMPOSITE_BACKEND,
                    ]
                )
            rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 1)
        self.assertEqual(rows[0]["error_code"], "identity_resolver_not_available")
        self.assertEqual(rows[0]["backend"], "identity.not_available")

    def test_signed_direct_selector_crosses_composite_boundary_only_via_stdin_argument(self) -> None:
        signed = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=stdin-secret&xsec_source=pc_share"
        item = XiaohongshuAdapter().resolve_items(signed)[0]
        roots: list[Path] = []
        selectors: list[str | None] = []
        commands: list[list[str]] = []
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(
                _plugin_rows(media_kind="text"),
                temp_roots=roots,
                selectors=selectors,
                commands=commands,
            ),
        )
        result = provider.extract(_request(item))
        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(selectors, [signed])
        argv = " ".join(commands[0])
        self.assertIn("--selector_stdin true", argv)
        self.assertIn(item.id, argv)
        self.assertNotIn("xsec_token", argv)
        self.assertNotIn("stdin-secret", argv)
        self.assertNotIn("stdin-secret", str(result.metadata))

    def test_xhslink_selector_is_forwarded_via_stdin_not_argv(self) -> None:
        short_url = "http://xhslink.com/o/example-share"
        item = XiaohongshuAdapter().resolve_items(short_url)[0]
        selectors: list[str | None] = []
        commands: list[list[str]] = []
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(
                _plugin_rows(media_kind="text"),
                temp_roots=[],
                selectors=selectors,
                commands=commands,
            ),
        )
        result = provider.extract(_request(item))
        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(selectors, [short_url])
        argv = " ".join(commands[0])
        self.assertIn("stdin-selector", argv)
        self.assertNotIn(short_url, argv)


class XiaohongshuCompositeProviderTests(unittest.TestCase):
    def test_note_not_found_is_durable_terminal_gap_not_retryable_failure(self) -> None:
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=lambda _args, _timeout, _selector: (1, "", "note not found 404"),
        )

        result = provider.extract(_request(_item()))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.error_code, "xiaohongshu_not_found")
        self.assertTrue(result.components)
        self.assertTrue(all(component.status == "unavailable" for component in result.components))
        self.assertTrue(
            all(component.failure_class == "source_unavailable" for component in result.components)
        )
        self.assertTrue(all(component.terminal_reason for component in result.components))

    def test_ordered_image_ocr_and_not_applicable_video_cleanup_temp(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="image_text", image_count=2)
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True, enable_media_download_backends=True),
            runner=_asset_runner(rows, temp_roots=roots),
            image_ocr=lambda path, index: f"OCR text from image {index}: {path.name}",
        )

        result = provider.extract(_request(_item(source_type="image_text", media_kind="image_text")))

        self.assertEqual(result.status, "verified")
        self.assertIn("## Native Note Text", result.text)
        self.assertIn("## Image OCR", result.text)
        self.assertLess(result.text.index("image 1"), result.text.index("image 2"))
        self.assertIn("## Video Transcript", result.text)
        components = {component.id: component for component in result.components}
        self.assertEqual(components["native_text"].status, "verified")
        self.assertEqual(components["image_ocr"].status, "verified")
        self.assertEqual(components["video_transcript"].status, "not_applicable")
        self.assertEqual(components["image_ocr"].text_ref, "index.md#image-ocr")
        rendered = render_document(render_markdown_document(_item(source_type="image_text", media_kind="image_text"), result))
        self.assertIn("## Native Note Text", rendered)
        self.assertIn("## Image OCR", rendered)
        self.assertIn("## Video Transcript", rendered)
        self.assertTrue(roots)
        self.assertFalse(roots[0].exists())
        self.assertNotIn(str(roots[0]), str(result.metadata))

    def test_video_prefers_native_transcript_without_media_download(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="video", video_count=1, native_transcript="Native subtitle line")
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(rows, temp_roots=roots),
        )

        result = provider.extract(_request(_item(source_type="video", media_kind="video")))

        component = {value.id: value for value in result.components}["video_transcript"]
        self.assertEqual(result.status, "verified")
        self.assertEqual(component.status, "verified")
        self.assertEqual(component.source_level, "platform_transcript")
        self.assertIn("[00:00:00] Native subtitle line", result.text)

    def test_video_uses_asr_then_cleans_audio_and_video(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="video", video_count=1)
        asr = FakeAsr(text="ASR recovered speech")

        def command_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            Path(command[-1]).write_bytes(b"audio")
            return 0, "", ""

        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(
                enable_browser_backends=True,
                enable_media_download_backends=True,
                enable_asr_backends=True,
                enable_cloud_backends=True,
            ),
            runner=_asset_runner(rows, temp_roots=roots),
            command_runner=command_runner,
            asr_client=asr,
            hard_subtitle_ocr=lambda _path: self.fail("hard subtitle OCR should not run after successful ASR"),
        )
        result = provider.extract(_request(_item(source_type="video", media_kind="video")))

        component = {value.id: value for value in result.components}["video_transcript"]
        self.assertEqual(component.status, "verified")
        self.assertEqual(component.source_level, "media_asr")
        self.assertIn("ASR recovered speech", result.text)
        self.assertEqual(len(asr.calls), 1)
        self.assertFalse(roots[0].exists())

    def test_asr_failure_falls_back_to_hard_subtitle_ocr(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="video", video_count=1)

        def command_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            Path(command[-1]).write_bytes(b"audio")
            return 0, "", ""

        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(
                enable_browser_backends=True,
                enable_media_download_backends=True,
                enable_asr_backends=True,
                enable_cloud_backends=True,
            ),
            runner=_asset_runner(rows, temp_roots=roots),
            command_runner=command_runner,
            asr_client=FakeAsr(error=RuntimeError("no meaningful speech")),
            hard_subtitle_ocr=lambda _path: "[00:02] visible subtitle",
        )
        result = provider.extract(_request(_item(source_type="video", media_kind="video")))

        component = {value.id: value for value in result.components}["video_transcript"]
        self.assertEqual(component.status, "verified")
        self.assertEqual(component.backend, XIAOHONGSHU_HARD_SUBTITLE_BACKEND)
        self.assertIn("visible subtitle", result.text)

    def test_no_speech_and_empty_hard_subtitle_is_terminal_unavailable(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="video", video_count=1)

        def command_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            Path(command[-1]).write_bytes(b"audio")
            return 0, "", ""

        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(
                enable_browser_backends=True,
                enable_media_download_backends=True,
                enable_asr_backends=True,
                enable_cloud_backends=True,
            ),
            runner=_asset_runner(rows, temp_roots=roots),
            command_runner=command_runner,
            asr_client=FakeAsr(error=RuntimeError("no meaningful speech")),
            hard_subtitle_ocr=lambda _path: "",
        )
        result = provider.extract(_request(_item(source_type="video", media_kind="video")))

        component = {value.id: value for value in result.components}["video_transcript"]
        self.assertEqual(result.status, "partial")
        self.assertEqual(component.status, "unavailable")
        self.assertEqual(component.failure_class, "source_unavailable")
        self.assertTrue(component.terminal_reason)

    def test_image_download_gate_is_retryable(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="image_text", image_count=1)
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=_asset_runner(rows, temp_roots=roots),
        )
        result = provider.extract(_request(_item(source_type="image_text", media_kind="image_text")))

        component = {value.id: value for value in result.components}["image_ocr"]
        self.assertEqual(result.status, "partial")
        self.assertEqual(component.status, "retryable")
        self.assertEqual(component.failure_class, "user_resolvable")

    def test_failed_first_image_keeps_second_image_slot_and_is_retryable(self) -> None:
        roots: list[Path] = []
        rows = _plugin_rows(media_kind="image_text", image_count=2)
        rows[1]["asset_status"] = "failed"
        rows[1]["asset_error_code"] = "asset_download_timeout"
        rows[1]["local_path"] = ""
        calls: list[tuple[str, int]] = []

        def image_ocr(path: Path, index: int) -> str:
            calls.append((path.name, index))
            return f"Readable image slot {index}"

        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True, enable_media_download_backends=True),
            runner=_asset_runner(rows, temp_roots=roots),
            image_ocr=image_ocr,
        )
        result = provider.extract(_request(_item(source_type="image_text", media_kind="image_text")))
        component = {value.id: value for value in result.components}["image_ocr"]
        self.assertEqual(component.status, "retryable")
        self.assertEqual(component.error_code, "asset_download_timeout")
        self.assertEqual(calls, [("image_002.jpg", 2)])
        self.assertIn("### Image 1", result.text)
        self.assertIn("### Image 2", result.text)
        self.assertIn("Readable image slot 2", result.text)

    def test_image_ocr_exception_is_retryable_but_explicit_empty_is_unavailable(self) -> None:
        rows = _plugin_rows(media_kind="image_text", image_count=1)
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True, enable_media_download_backends=True),
            runner=_asset_runner(rows, temp_roots=[]),
            image_ocr=lambda _path, _index: (_ for _ in ()).throw(RuntimeError("OCR dependency missing")),
        )
        retryable = provider.extract(_request(_item(source_type="image_text", media_kind="image_text")))
        retryable_component = {value.id: value for value in retryable.components}["image_ocr"]
        self.assertEqual(retryable_component.status, "retryable")
        self.assertIn("dependency_missing", retryable_component.error_code or "")

        unavailable_provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True, enable_media_download_backends=True),
            runner=_asset_runner(rows, temp_roots=[]),
            image_ocr=lambda _path, _index: "",
        )
        unavailable = unavailable_provider.extract(_request(_item(source_type="image_text", media_kind="image_text")))
        unavailable_component = {value.id: value for value in unavailable.components}["image_ocr"]
        self.assertEqual(unavailable_component.status, "unavailable")
        self.assertEqual(unavailable_component.error_code, "xiaohongshu_image_ocr_empty")

    def test_video_download_and_asr_http_failures_are_retryable(self) -> None:
        download_rows = _plugin_rows(media_kind="video", video_count=1)
        download_rows[1]["asset_status"] = "failed"
        download_rows[1]["asset_error_code"] = "asset_download_http_error"
        download_rows[1]["local_path"] = ""
        download_provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(
                enable_browser_backends=True,
                enable_media_download_backends=True,
                enable_asr_backends=True,
                enable_cloud_backends=True,
            ),
            runner=_asset_runner(download_rows, temp_roots=[]),
            asr_client=FakeAsr(),
        )
        download_result = download_provider.extract(_request(_item(source_type="video", media_kind="video")))
        download_component = {value.id: value for value in download_result.components}["video_transcript"]
        self.assertEqual(download_component.status, "retryable")
        self.assertEqual(download_component.error_code, "asset_download_http_error")

        rows = _plugin_rows(media_kind="video", video_count=1)

        def command_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            Path(command[-1]).write_bytes(b"audio")
            return 0, "", ""

        asr_provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(
                enable_browser_backends=True,
                enable_media_download_backends=True,
                enable_asr_backends=True,
                enable_cloud_backends=True,
            ),
            runner=_asset_runner(rows, temp_roots=[]),
            command_runner=command_runner,
            asr_client=FakeAsr(error=RuntimeError("SiliconFlow ASR HTTP 503")),
            hard_subtitle_ocr=lambda _path: "",
        )
        asr_result = asr_provider.extract(_request(_item(source_type="video", media_kind="video")))
        asr_component = {value.id: value for value in asr_result.components}["video_transcript"]
        self.assertEqual(asr_component.status, "retryable")
        self.assertIn("http_error", asr_component.error_code or "")

    def test_command_is_canonical_and_never_passes_signed_values(self) -> None:
        command = build_opencli_xiaohongshu_note_assets_args(
            NOTE_URL + "?xsec_token=must-not-pass",
            output_dir=Path("C:/temp/xhs"),
            profile_id=PROFILE_ID,
            download=True,
        )
        serialized = " ".join(command)
        self.assertNotIn("xsec_token", serialized)
        self.assertNotIn("must-not-pass", serialized)
        self.assertIn(NOTE_URL, command)
        self.assertIn("--creator", command)
        self.assertIn("--timeout", command)
        self.assertIn("900", command)
        self.assertNotIn("--profile", command)


if __name__ == "__main__":
    unittest.main()
