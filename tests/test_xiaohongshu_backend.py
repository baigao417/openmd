"""Xiaohongshu OpenCLI runtime provider regression tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openmd.backends.base import BackendExtractRequest, DiscoveryRequest
from openmd.backends.executor import SkeletonBackendProvider, SkeletonDiscoveryProvider
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
)
from openmd.backends.youtube_discovery import _windows_batch_command_line
from openmd.backends.xiaohongshu import (
    OPENCLI_XIAOHONGSHU_NOTE_BACKEND,
    OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
    OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND,
    XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
    OpenCliOpenmdXiaohongshuCreatorNotesProvider,
    OpenCliXiaohongshuNoteProvider,
    OpenCliXiaohongshuUserDiscoveryProvider,
    build_opencli_openmd_xiaohongshu_creator_notes_args,
    build_opencli_xiaohongshu_note_args,
    build_opencli_xiaohongshu_user_args,
    parse_opencli_xiaohongshu_user_output,
)
from openmd.core.manifest import discovery_manifest_row
from openmd.core.models import ContentItem


SENSITIVE_QUERY_KEY = "xsec" + "_token"
DEFAULT_SIGNED_VALUE = "-".join(("signed", "value"))


def _signed_note_url(note_id: str = "note123", value: str = DEFAULT_SIGNED_VALUE) -> str:
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_source=pc_share&{SENSITIVE_QUERY_KEY}={value}"
    )


def _unsigned_note_url(note_id: str = "note123") -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_source=pc_share"


def _user_url(user_id: str = "user123") -> str:
    return f"https://www.xiaohongshu.com/user/profile/{user_id}"


def _fixture_full_rows() -> list[dict[str, object]]:
    spec_path = Path(__file__).parent / "fixtures" / "xiaohongshu" / "full-profile-snapshots.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    end = int(spec["pages"][-1]["end"])
    rows: list[dict[str, object]] = []
    for ordinal in range(1, end + 1):
        note_id = f"{ordinal:024x}"
        media_kind = "video" if ordinal % 3 == 0 else "image_text"
        rows.append(
            {
                "index": ordinal,
                "note_id": note_id,
                "title": f"Fixture note {ordinal}",
                "author": "Fixture creator",
                "type": media_kind,
                "url": f"https://www.xiaohongshu.com/discovery/item/{note_id}",
                "page_count": 3,
                "scroll_count": 2,
                "stable_round_count": 0,
                "empty_snapshot_count": 0,
                "duplicate_snapshot_count": 0,
                "anomaly_count": 0,
                "source_exhausted": True,
                "explicit_exhaustion": True,
                "exhaustion_reason": "pinia_has_more_false",
                "store_source": "pinia:user",
            }
        )
    return rows


def _request(item: ContentItem, backend: str = OPENCLI_XIAOHONGSHU_NOTE_BACKEND) -> BackendExtractRequest:
    return BackendExtractRequest(item=item, backend_chain=(backend,), allow_fallback=False)


class XiaohongshuRuntimeGateTests(unittest.TestCase):
    def test_windows_batch_command_line_quotes_signed_url_query_params(self) -> None:
        command_line = _windows_batch_command_line(
            [
                r"C:\Users\example\AppData\Roaming\npm\opencli.cmd",
                "xiaohongshu",
                "note",
                _signed_note_url(),
                "-f",
                "json",
            ]
        )

        self.assertIn(f'"{_signed_note_url()}"', command_line)
        self.assertNotIn("& -f", command_line)
        self.assertTrue(command_line.startswith('"C:\\Users\\example'))

    def test_browser_backend_gate_registers_note_provider_extension(self) -> None:
        safe_registry = create_runtime_backend_provider_registry("xiaohongshu", BackendRuntimeOptions())
        safe_provider = safe_registry.provider_for(OPENCLI_XIAOHONGSHU_NOTE_BACKEND)
        self.assertIsInstance(safe_provider, SkeletonBackendProvider)
        self.assertNotIsInstance(safe_provider, OpenCliXiaohongshuNoteProvider)

        browser_registry = create_runtime_backend_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        provider = browser_registry.provider_for(OPENCLI_XIAOHONGSHU_NOTE_BACKEND)
        self.assertIsInstance(provider, OpenCliXiaohongshuNoteProvider)

    def test_browser_backend_gate_registers_user_discovery_provider_extension(self) -> None:
        request = DiscoveryRequest(
            input_url=_user_url(),
            platform="xiaohongshu",
            limit=2,
            allow_all=False,
        )
        safe_registry = create_runtime_discovery_provider_registry("xiaohongshu", BackendRuntimeOptions())
        safe_provider = safe_registry.provider_for(request)
        self.assertIsInstance(safe_provider, SkeletonDiscoveryProvider)
        self.assertNotIsInstance(safe_provider, OpenCliXiaohongshuUserDiscoveryProvider)

        browser_registry = create_runtime_discovery_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        provider = browser_registry.provider_for(request)
        self.assertIsInstance(provider, OpenCliXiaohongshuUserDiscoveryProvider)

    def test_explicit_full_provider_registers_only_behind_browser_gate(self) -> None:
        request = DiscoveryRequest(
            input_url=_user_url("683e522f000000001d008dba"),
            platform="xiaohongshu",
            limit=None,
            allow_all=True,
        )
        safe_registry = create_runtime_discovery_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(
                requested_discovery_provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            ),
        )
        self.assertIsInstance(
            safe_registry.provider_for(
                request,
                requested_provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            ),
            SkeletonDiscoveryProvider,
        )

        browser_registry = create_runtime_discovery_provider_registry(
            "xiaohongshu",
            BackendRuntimeOptions(
                enable_browser_backends=True,
                requested_discovery_provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            ),
        )
        self.assertIsInstance(
            browser_registry.provider_for(
                request,
                requested_provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            ),
            OpenCliOpenmdXiaohongshuCreatorNotesProvider,
        )


class XiaohongshuNoteProviderTests(unittest.TestCase):
    def test_note_success_normalizes_json_without_persisting_raw_streams(self) -> None:
        calls: list[tuple[list[str], int]] = []
        note_url = _signed_note_url()

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "data": {
                            "note_id": "note123",
                            "title": "Fixture Note",
                            "desc": "Raw Xiaohongshu note body.",
                            "user": {"nickname": "Fixture Author"},
                            "publish_time": "2026-06-17",
                        }
                    }
                ),
                "",
            )

        provider = OpenCliXiaohongshuNoteProvider(runner=fake_runner, timeout=7)
        item = ContentItem(
            platform="xiaohongshu",
            input_type="single",
            url=note_url,
            source_type="mixed",
        )

        result = provider.extract(_request(item))

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, OPENCLI_XIAOHONGSHU_NOTE_BACKEND)
        self.assertEqual(result.title, "Fixture Note")
        self.assertEqual(result.author, "Fixture Author")
        self.assertIn("Raw Xiaohongshu note body.", result.text)
        self.assertEqual(calls, [(build_opencli_xiaohongshu_note_args(note_url), 7)])
        self.assertNotIn("stdout", result.metadata)
        self.assertNotIn("stderr", result.metadata)
        self.assertNotIn(SENSITIVE_QUERY_KEY, str(result.metadata))
        self.assertNotIn(DEFAULT_SIGNED_VALUE, str(result.metadata))

    def test_note_missing_signed_query_returns_structured_failure_without_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, "unexpected", ""

        provider = OpenCliXiaohongshuNoteProvider(runner=fake_runner)
        item = ContentItem(
            platform="xiaohongshu",
            input_type="single",
            url=_unsigned_note_url(),
            source_type="mixed",
        )

        result = provider.extract(_request(item))

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.source_level, "partial_extraction")
        self.assertEqual(result.error_code, "xiaohongshu_signed_url_required")
        self.assertEqual(calls, [])
        self.assertNotIn(SENSITIVE_QUERY_KEY, str(result.metadata))

    def test_sensitive_query_is_redacted_from_failure_summary_and_metadata(self) -> None:
        sensitive_value = "-".join(("do", "not", "leak"))
        note_url = _signed_note_url(value=sensitive_value)

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 1, "", f"failed for {note_url}"

        provider = OpenCliXiaohongshuNoteProvider(runner=fake_runner)
        item = ContentItem(
            platform="xiaohongshu",
            input_type="single",
            url=note_url,
            source_type="mixed",
        )

        result = provider.extract(_request(item))

        self.assertFalse(result.has_usable_text)
        summary = result.error_summary or ""
        metadata_text = str(result.metadata)
        self.assertNotIn(SENSITIVE_QUERY_KEY, summary)
        self.assertNotIn(sensitive_value, summary)
        self.assertNotIn(SENSITIVE_QUERY_KEY, metadata_text)
        self.assertNotIn(sensitive_value, metadata_text)


class XiaohongshuUserDiscoveryTests(unittest.TestCase):
    def test_user_discovery_uses_explicit_limit_command_shape(self) -> None:
        calls: list[tuple[list[str], int]] = []
        user_url = _user_url()
        note_id = "000000000000000000000456"
        note_url = _signed_note_url(note_id)

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "note_id": note_id,
                                "title": "Bounded Note",
                                "type": "normal",
                                "url": note_url,
                                "user": {"nickname": "Fixture User"},
                                "publish_time": "2026-06-17",
                            }
                        ]
                    }
                ),
                "",
            )

        provider = OpenCliXiaohongshuUserDiscoveryProvider(runner=fake_runner, timeout=9)
        result = provider.discover(
            DiscoveryRequest(
                input_url=user_url,
                platform="xiaohongshu",
                limit=3,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(calls, [(build_opencli_xiaohongshu_user_args(user_url, limit=3), 9)])
        self.assertEqual(result.provider, OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND)
        self.assertEqual(result.metadata["limit"], 3)
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.platform, "xiaohongshu")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "image_text")
        self.assertEqual(item.id, note_id)
        self.assertEqual(item.url, f"https://www.xiaohongshu.com/discovery/item/{note_id}")
        self.assertEqual(item.title, "Bounded Note")
        self.assertEqual(item.author, "Fixture User")
        self.assertEqual(item.raw_meta["discovery_provider"], OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND)
        self.assertEqual(item.raw_meta["runtime_note_url"], note_url)
        self.assertEqual(item.raw_meta["extraction_contract_version"], XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION)
        self.assertEqual(item.raw_meta["required_components"], ["native_text", "image_ocr"])

    def test_default_user_provider_routes_all_to_project_plugin(self) -> None:
        calls: list[tuple[list[str], int]] = []
        profile_url = _user_url("683e522f000000001d008dba")

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, json.dumps(_fixture_full_rows()), ""

        result = OpenCliXiaohongshuUserDiscoveryProvider(
            runner=fake_runner,
            full_timeout=77,
        ).discover(
            DiscoveryRequest(
                input_url=profile_url,
                platform="xiaohongshu",
                limit=None,
                allow_all=True,
            )
        )
        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND)
        self.assertEqual(len(result.items), 65)
        self.assertEqual(
            calls,
            [(build_opencli_openmd_xiaohongshu_creator_notes_args(profile_url), 77)],
        )


class XiaohongshuFullDiscoveryTests(unittest.TestCase):
    def test_full_command_accepts_raw_user_id_and_emits_canonical_profile(self) -> None:
        user_id = "683e522f000000001d008dba"
        command = build_opencli_openmd_xiaohongshu_creator_notes_args(user_id)
        self.assertEqual(command[3], _user_url(user_id))

    def test_full_provider_discovers_more_than_50_and_preserves_terminal_metadata(self) -> None:
        calls: list[tuple[list[str], int, str]] = []
        profile_url = _user_url("683e522f000000001d008dba")
        signed_profile_url = profile_url + f"?{SENSITIVE_QUERY_KEY}=profile-secret&xsec_source=pc_feed"

        def fake_input_runner(
            args: list[str],
            *,
            timeout: int = 120,
            stdin_text: str | None = None,
        ) -> tuple[int, str, str]:
            calls.append((args, timeout, stdin_text or ""))
            rows = _fixture_full_rows()
            for row in rows:
                row["runtime_note_url"] = (
                    f"https://www.xiaohongshu.com/user/profile/683e522f000000001d008dba/{row['note_id']}"
                    f"?{SENSITIVE_QUERY_KEY}=note-{row['note_id']}&xsec_source=pc_user"
                )
            return 0, json.dumps(rows), ""

        provider = OpenCliOpenmdXiaohongshuCreatorNotesProvider(
            input_runner=fake_input_runner,
            timeout=99,
        )
        result = provider.discover(
            DiscoveryRequest(
                input_url=signed_profile_url,
                platform="xiaohongshu",
                limit=None,
                allow_all=True,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(len(result.items), 65)
        self.assertEqual(len({item.id for item in result.items}), 65)
        self.assertEqual(result.metadata["source_exhausted"], True)
        self.assertEqual(result.metadata["explicit_exhaustion"], True)
        self.assertEqual(result.metadata["page_count"], 3)
        self.assertEqual(result.metadata["scroll_count"], 2)
        self.assertEqual(
            calls,
            [(
                build_opencli_openmd_xiaohongshu_creator_notes_args(
                    profile_url,
                    selector_stdin=True,
                ),
                99,
                signed_profile_url,
            )],
        )
        self.assertNotIn("profile-secret", " ".join(calls[0][0]))
        self.assertIn("runtime_note_url", result.items[0].raw_meta)
        self.assertNotIn("runtime_profile_selector", result.items[0].raw_meta)
        manifest_row = discovery_manifest_row(
            result.items[0],
            collection_url=profile_url,
            provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            limit=None,
        )
        self.assertNotIn("profile-secret", json.dumps(manifest_row))
        self.assertNotIn("runtime_profile_selector", json.dumps(manifest_row))
        self.assertNotIn("runtime_note_url", json.dumps(manifest_row))
        self.assertNotIn(SENSITIVE_QUERY_KEY, json.dumps(manifest_row))

        video = result.items[2]
        image = result.items[0]
        self.assertEqual(video.source_type, "video")
        self.assertEqual(video.raw_meta["required_components"], ["native_text", "video_transcript"])
        self.assertEqual(image.source_type, "image_text")
        self.assertEqual(image.raw_meta["required_components"], ["native_text", "image_ocr"])
        self.assertEqual(
            image.raw_meta["extraction_contract_version"],
            XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
        )

    def test_full_provider_rejects_stable_only_rows_as_not_explicitly_exhausted(self) -> None:
        rows = _fixture_full_rows()
        for row in rows:
            row["source_exhausted"] = False
            row["explicit_exhaustion"] = False
            row["exhaustion_reason"] = "stable_rounds"

        provider = OpenCliOpenmdXiaohongshuCreatorNotesProvider(
            runner=lambda _args, timeout=120: (0, json.dumps(rows), ""),
        )
        result = provider.discover(
            DiscoveryRequest(
                input_url=_user_url("683e522f000000001d008dba"),
                platform="xiaohongshu",
                limit=None,
                allow_all=True,
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "xiaohongshu_source_not_explicitly_exhausted")

    def test_parser_deduplicates_note_ids_and_full_rows_have_no_runtime_signed_url(self) -> None:
        rows = _fixture_full_rows()
        rows.append(dict(rows[0]))
        items = parse_opencli_xiaohongshu_user_output(
            json.dumps(rows),
            collection_url=_user_url("683e522f000000001d008dba"),
            discovery_provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            include_transient=True,
            include_runtime_note_url=False,
        )

        self.assertEqual(len(items), 65)
        self.assertNotIn("runtime_note_url", items[0].raw_meta)
        plugin_stdout = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(SENSITIVE_QUERY_KEY, plugin_stdout)
        self.assertNotIn("runtime_note_url", plugin_stdout)
        self.assertNotIn("token", plugin_stdout.lower())
        row = discovery_manifest_row(
            items[0],
            collection_url=_user_url("683e522f000000001d008dba"),
            provider=OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            limit=None,
        )
        serialized = json.dumps(row, ensure_ascii=False)
        self.assertNotIn(SENSITIVE_QUERY_KEY, serialized)
        self.assertNotIn("runtime_note_url", serialized)
        self.assertNotIn(SENSITIVE_QUERY_KEY, items[0].url)


if __name__ == "__main__":
    unittest.main()
