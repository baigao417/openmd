"""Xiaoyuzhou runtime provider regression tests with fake runners."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import (
    BackendExecutor,
    BackendProviderRegistry,
    DiscoveryExecutor,
    SkeletonBackendProvider,
)
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.backends.registry import create_runtime_discovery_provider_registry
from openmd.backends.xiaoyuzhou import (
    OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
    OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND,
    PODWISE_TRANSCRIPT_BACKEND,
    OpenCliXiaoyuzhouPodcastEpisodesProvider,
    OpenCliXiaoyuzhouTranscriptProvider,
    PodwiseTranscriptProvider,
    build_opencli_xiaoyuzhou_podcast_episodes_args,
    build_opencli_xiaoyuzhou_transcript_args,
    build_opencli_web_read_args,
    build_podwise_get_transcript_args,
    build_podwise_search_args,
    extract_xiaoyuzhou_podcast_id,
    parse_xiaoyuzhou_episode_items,
)
from openmd.backends.base import DiscoveryRequest
from openmd.core.models import ContentItem


SENSITIVE_ERROR_TERMS = (
    "access_token",
    "refresh_token",
    "authorization",
    "headers",
    "cookie",
    "raw_transcript",
    "secret",
    "bearer",
)


def _xiaoyuzhou_item(url: str = "https://www.xiaoyuzhoufm.com/episode/6a1cdbc1ac7bdb080c33d96d") -> ContentItem:
    return ContentItem(
        platform="xiaoyuzhou",
        input_type="single",
        url=url,
        source_type="audio",
    )


def _request(item: ContentItem, backend: str) -> BackendExtractRequest:
    return BackendExtractRequest(item=item, backend_chain=(backend,), allow_fallback=False)


def _write_android_credentials(path: Path, *, device_id: str = "device-fixture") -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "dummy-access",
                "refresh_token": "dummy-refresh",
                "device_id": device_id,
                "device_properties": json.dumps(
                    {
                        "uuid": device_id,
                        "android_id": "android-fixture",
                        "oaid": "oaid-fixture",
                        "vaid": "vaid-fixture",
                        "aaid": "aaid-fixture",
                    }
                ),
            }
        ),
        encoding="utf-8",
    )


def _write_opencli_ios_credentials(path: Path, *, device_id: str = "ios-device-fixture") -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "dummy-access",
                "refresh_token": "dummy-refresh",
                "device_id": device_id,
                "device_properties": json.dumps(
                    {
                        "idfa": "00000000-0000-0000-0000-000000000000",
                        "idfv": "idfv-fixture",
                    }
                ),
            }
        ),
        encoding="utf-8",
    )


class XiaoyuzhouRuntimeGateTests(unittest.TestCase):
    def test_browser_backend_gate_registers_xiaoyuzhou_runtime_providers(self) -> None:
        safe_registry = create_runtime_backend_provider_registry("xiaoyuzhou", BackendRuntimeOptions())
        gated_provider = safe_registry.provider_for(OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND)
        gated_podwise = safe_registry.provider_for(PODWISE_TRANSCRIPT_BACKEND)

        self.assertIsInstance(gated_provider, SkeletonBackendProvider)
        self.assertIsInstance(gated_podwise, SkeletonBackendProvider)

        browser_registry = create_runtime_backend_provider_registry(
            "xiaoyuzhou",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        provider = browser_registry.provider_for(OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND)
        podwise_provider = browser_registry.provider_for(PODWISE_TRANSCRIPT_BACKEND)

        self.assertIsInstance(provider, OpenCliXiaoyuzhouTranscriptProvider)
        self.assertIsInstance(podwise_provider, PodwiseTranscriptProvider)

    def test_browser_backend_gate_registers_xiaoyuzhou_discovery_provider(self) -> None:
        request = DiscoveryRequest(
            input_url="https://www.xiaoyuzhoufm.com/podcast/podcast-fixture",
            platform="xiaoyuzhou",
            limit=2,
            allow_all=False,
        )

        safe_registry = create_runtime_discovery_provider_registry(
            "xiaoyuzhou",
            BackendRuntimeOptions(
                requested_discovery_provider=OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
            ),
        )
        safe_provider = safe_registry.provider_for(
            request,
            requested_provider=OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
        )

        browser_registry = create_runtime_discovery_provider_registry(
            "xiaoyuzhou",
            BackendRuntimeOptions(
                requested_discovery_provider=OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
                enable_browser_backends=True,
            ),
        )
        browser_provider = browser_registry.provider_for(
            request,
            requested_provider=OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
        )

        self.assertEqual(getattr(safe_provider, "name"), "skeleton.discovery")
        self.assertIsInstance(browser_provider, OpenCliXiaoyuzhouPodcastEpisodesProvider)


class OpenCliXiaoyuzhouTranscriptProviderTests(unittest.TestCase):
    def test_missing_author_fetches_public_episode_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_android_credentials(credentials_path)
            calls = []

            def runner(command, *, timeout):
                calls.append(command)
                if command[2] == "episode":
                    return 0, json.dumps({"ok": True, "data": [{"podcast": "播客名称", "title": "节目标题", "description": "页面节目简介"}]}), ""
                output = Path(command[command.index("--output") + 1])
                (output / "transcript.txt").write_text("[00:00:00] original transcript", encoding="utf-8")
                return 0, '{"ok":true}', ""

            provider = OpenCliXiaoyuzhouTranscriptProvider(runner=runner, credential_path=credentials_path)
            result = provider.extract(_request(_xiaoyuzhou_item(), provider.name))
            self.assertEqual(result.author, "播客名称")
            self.assertEqual(result.source_level, "native_caption")
            self.assertTrue(result.text.startswith("# 节目标题\n\n节目简介（来源：页面上的节目描述）：\n页面节目简介\n\n"))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1:3], ["xiaoyuzhou", "episode"])

    def test_episode_metadata_failure_does_not_discard_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_android_credentials(credentials_path)

            def runner(command, *, timeout):
                if command[2] == "episode":
                    return 124, "", "metadata timed out"
                output = Path(command[command.index("--output") + 1])
                (output / "transcript.txt").write_text("[00:00:00] original transcript", encoding="utf-8")
                return 0, '{"ok":true}', ""

            provider = OpenCliXiaoyuzhouTranscriptProvider(runner=runner, credential_path=credentials_path)
            result = provider.extract(_request(_xiaoyuzhou_item(), provider.name))
            self.assertTrue(result.has_usable_text)
            self.assertIsNone(result.author)

    def test_transcript_keeps_segment_timestamps_and_reported_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_android_credentials(credentials_path)

            def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                output_dir = Path(command[command.index("--output") + 1]) / "episode"
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "transcript.txt").write_text("first second", encoding="utf-8")
                (output_dir / "transcript.json").write_text(
                    json.dumps(
                        [
                            {"text": "first useful line", "startMs": 0},
                            {"text": "last useful line", "startMs": 8616120},
                        ]
                    ),
                    encoding="utf-8",
                )
                envelope = {"ok": True, "data": [{"title": "Real episode title", "podcast": "Fixture podcast", "status": "success"}]}
                return 0, json.dumps(envelope), ""

            provider = OpenCliXiaoyuzhouTranscriptProvider(
                runner=fake_runner,
                credential_path=credentials_path,
                min_transcript_chars=1,
            )
            result = provider.extract(_request(_xiaoyuzhou_item(), OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND))

        self.assertEqual(result.title, "Real episode title")
        self.assertEqual(result.text, "[00:00:00] first useful line\n[02:23:36] last useful line\n")

    def test_opencli_transcript_success_reads_temp_text_file(self) -> None:
        calls: list[tuple[list[str], int]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_android_credentials(credentials_path)

            def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                calls.append((command, timeout))
                output_dir = Path(command[command.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "transcript.txt").write_text(
                    "[00:00:00] fixture transcript\n[00:00:05] useful body",
                    encoding="utf-8",
                )
                return 0, '{"ok": true, "data": [{"podcast": "Fixture podcast"}]}', ""

            provider = OpenCliXiaoyuzhouTranscriptProvider(
                runner=fake_runner,
                credential_path=credentials_path,
                timeout=7,
            )
            item = _xiaoyuzhou_item()

            result = provider.extract(_request(item, OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_caption")
        self.assertEqual(result.backend, OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND)
        self.assertIn("[00:00:05] useful body", result.text)
        self.assertEqual(result.metadata["episode_id"], "6a1cdbc1ac7bdb080c33d96d")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 7)
        self.assertEqual(
            calls[0][0][:4],
            ["opencli", "xiaoyuzhou", "transcript", "6a1cdbc1ac7bdb080c33d96d"],
        )
        self.assertEqual(
            calls[0][0],
            build_opencli_xiaoyuzhou_transcript_args(
                "6a1cdbc1ac7bdb080c33d96d",
                output_dir=calls[0][0][calls[0][0].index("--output") + 1],
            ),
        )
        for raw_key in ("stdout", "stderr", "raw_stdout", "raw_stderr", "request_headers"):
            self.assertNotIn(raw_key, result.metadata)

    def test_credential_shape_failure_does_not_call_opencli_or_leak_field_names(self) -> None:
        calls: list[list[str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            credentials_path.write_text(
                json.dumps({"access_token": "dummy-access", "device_id": "device-fixture"}),
                encoding="utf-8",
            )

            def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                calls.append(command)
                return 99, "", "unexpected command"

            provider = OpenCliXiaoyuzhouTranscriptProvider(
                runner=fake_runner,
                credential_path=credentials_path,
            )
            result = provider.extract(_request(_xiaoyuzhou_item(), OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND))

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "xiaoyuzhou_credentials_incomplete")
        self.assertEqual(calls, [])
        self._assert_no_sensitive_error_terms(result.error_summary)

    def test_opencli_transcript_accepts_opencli_ios_credentials_without_headers(self) -> None:
        calls: list[list[str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_opencli_ios_credentials(credentials_path)

            def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                calls.append(command)
                output_dir = Path(command[command.index("--output") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "transcript.txt").write_text(
                    "[00:00:00] iOS credential fixture transcript",
                    encoding="utf-8",
                )
                return 0, '{"ok": true, "data": [{"podcast": "Fixture podcast"}]}', ""

            provider = OpenCliXiaoyuzhouTranscriptProvider(
                runner=fake_runner,
                credential_path=credentials_path,
            )
            result = provider.extract(_request(_xiaoyuzhou_item(), OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.error_code, None)
        self.assertEqual(len(calls), 1)

    def test_opencli_failure_returns_sanitized_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            _write_android_credentials(credentials_path)

            def failing_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                return (
                    2,
                    "stdout access_token=secret raw_transcript=secret",
                    "refresh_token=secret authorization Bearer secret headers cookie raw_transcript",
                )

            provider = OpenCliXiaoyuzhouTranscriptProvider(
                runner=failing_runner,
                credential_path=credentials_path,
            )
            result = provider.extract(_request(_xiaoyuzhou_item(), OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND))

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "backend_exit_2")
        self.assertEqual(result.metadata["exit_code"], 2)
        self._assert_no_sensitive_error_terms(result.error_summary)

    def _assert_no_sensitive_error_terms(self, value: str | None) -> None:
        text = (value or "").lower()
        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, text)


class OpenCliXiaoyuzhouPodcastEpisodesProviderTests(unittest.TestCase):
    def test_podcast_id_extraction_supports_url_and_plain_id(self) -> None:
        self.assertEqual(
            extract_xiaoyuzhou_podcast_id("https://www.xiaoyuzhoufm.com/podcast/abc123"),
            "abc123",
        )
        self.assertEqual(extract_xiaoyuzhou_podcast_id("abc123"), "abc123")

    def test_parse_episode_json_builds_audio_content_items(self) -> None:
        payload = json.dumps(
            {
                "items": [
                    {
                        "eid": "ep1",
                        "title": "Episode 1",
                        "duration": "01:00",
                        "plays": 42,
                        "date": "2026-06-23",
                    }
                ]
            }
        )

        items = parse_xiaoyuzhou_episode_items(payload, podcast_id="pod1")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].platform, "xiaoyuzhou")
        self.assertEqual(items[0].input_type, "collection")
        self.assertEqual(items[0].source_type, "audio")
        self.assertEqual(items[0].id, "ep1")
        self.assertEqual(items[0].url, "https://www.xiaoyuzhoufm.com/episode/ep1")
        self.assertEqual(items[0].raw_meta["podcast_id"], "pod1")

    def test_provider_success_runs_opencli_podcast_episodes_and_limits_items(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((command, timeout))
            return (
                0,
                json.dumps(
                    [
                        {"eid": "ep1", "title": "Episode 1"},
                        {"eid": "ep2", "title": "Episode 2"},
                    ]
                ),
                "",
            )

        provider = OpenCliXiaoyuzhouPodcastEpisodesProvider(
            runner=fake_runner,
            timeout=9,
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.xiaoyuzhoufm.com/podcast/pod1",
                platform="xiaoyuzhou",
                limit=1,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].id, "ep1")
        self.assertEqual(
            calls,
            [(build_opencli_xiaoyuzhou_podcast_episodes_args("pod1", limit=1), 9)],
        )

    def test_provider_failure_sanitizes_sensitive_error(self) -> None:
        def fake_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 2, "", "authorization Bearer secret cookie=secret raw_transcript=secret"

        provider = OpenCliXiaoyuzhouPodcastEpisodesProvider(runner=fake_runner)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.xiaoyuzhoufm.com/podcast/pod1",
                platform="xiaoyuzhou",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "backend_exit_2")
        text = (result.error_summary or "").lower()
        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, text)


class PodwiseXiaoyuzhouFallbackProviderTests(unittest.TestCase):
    def test_backend_executor_falls_back_to_already_processed_podwise_transcript(self) -> None:
        opencli_calls: list[tuple[list[str], int, str | None]] = []
        podwise_calls: list[tuple[list[str], int]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            credentials_path = Path(tmpdir) / "xiaoyuzhou.json"
            credentials_path.write_text(
                json.dumps({"access_token": "dummy-access", "device_id": "device-fixture"}),
                encoding="utf-8",
            )

            def fake_opencli_runner(
                command: list[str],
                *,
                timeout: int = 120,
                cwd: str | Path | None = None,
            ) -> tuple[int, str, str]:
                opencli_calls.append((command, timeout, str(cwd) if cwd is not None else None))
                return 0, "# Fixture Episode\n\nIntro text.", ""

            def fake_podwise_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                podwise_calls.append((command, timeout))
                if command[:3] == ["podwise", "search", "episode"]:
                    return 0, "https://podwise.ai/dashboard/episodes/12345", ""
                if command[:3] == ["podwise", "get", "transcript"]:
                    return 0, "[00:00:00] processed podwise transcript", ""
                return 99, "", "unexpected command"

            provider_registry = BackendProviderRegistry(
                {
                    OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND: OpenCliXiaoyuzhouTranscriptProvider(
                        runner=fake_opencli_runner,
                        credential_path=credentials_path,
                    ),
                    PODWISE_TRANSCRIPT_BACKEND: PodwiseTranscriptProvider(
                        opencli_runner=fake_opencli_runner,
                        podwise_runner=fake_podwise_runner,
                    ),
                }
            )
            executor = BackendExecutor(provider_registry=provider_registry)
            result = executor.extract(
                _xiaoyuzhou_item(),
                (OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND, PODWISE_TRANSCRIPT_BACKEND),
                allow_fallback=True,
            )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.backend, PODWISE_TRANSCRIPT_BACKEND)
        self.assertIn("processed podwise transcript", result.text)
        self.assertEqual(
            opencli_calls,
            [
                (
                    build_opencli_web_read_args("https://www.xiaoyuzhoufm.com/episode/6a1cdbc1ac7bdb080c33d96d"),
                    180,
                    opencli_calls[0][2],
                )
            ],
        )
        self.assertEqual(
            podwise_calls,
            [
                (build_podwise_search_args("Fixture Episode"), 120),
                (build_podwise_get_transcript_args("https://podwise.ai/dashboard/episodes/12345"), 180),
            ],
        )
        all_commands = [command for command, _timeout in podwise_calls]
        self.assertFalse(any(command[:2] == ["podwise", "process"] for command in all_commands))

    def test_podwise_not_processed_guard_never_runs_paid_process(self) -> None:
        podwise_calls: list[list[str]] = []

        def fake_podwise_runner(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            podwise_calls.append(command)
            return 1, "", "This episode has not been processed yet. Run podwise process first."

        provider = PodwiseTranscriptProvider(podwise_runner=fake_podwise_runner)
        item = _xiaoyuzhou_item("https://podwise.ai/dashboard/episodes/12345")

        result = provider.extract(_request(item, PODWISE_TRANSCRIPT_BACKEND))

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "podwise_episode_not_processed")
        self.assertIn("does not run", result.error_summary or "")
        self.assertEqual(
            podwise_calls,
            [build_podwise_get_transcript_args("https://podwise.ai/dashboard/episodes/12345")],
        )
        self.assertFalse(any(command[:2] == ["podwise", "process"] for command in podwise_calls))


if __name__ == "__main__":
    unittest.main()
