from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from openmd.cli import main as cli_main
from openmd.core.capabilities import (
    APPROVED_CAPABILITIES,
    get_capability,
    validate_capability_manifest,
)
from openmd.core.planner import build_plan


class CapabilityContractTests(unittest.TestCase):
    def test_tier_and_state_are_independent_and_paused_sources_fail_closed(self) -> None:
        self.assertEqual(get_capability("youtube").tier, "A")
        self.assertEqual(get_capability("youtube").state, "partial")
        self.assertEqual(get_capability("tiktok").tier, "C")
        self.assertEqual(get_capability("tiktok").state, "paused")
        self.assertEqual(get_capability("qq_group").state, "paused")

        with self.assertRaisesRegex(ValueError, "duplicated"):
            validate_capability_manifest(APPROVED_CAPABILITIES + (APPROVED_CAPABILITIES[0],))

    def test_plan_is_pure_and_unbound_routes_are_not_execution_ready(self) -> None:
        with mock.patch("openmd.backends.registry.create_runtime_backend_provider_registry") as registry:
            result = build_plan("https://example.com/article")
        registry.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.detected_source, "web_article")
        self.assertFalse(result.execution_ready)
        self.assertIn("no_network", result.side_effects)

        unbound = build_plan("https://example.com/fixture.mp4")
        self.assertTrue(unbound.ok)
        self.assertFalse(unbound.execution_ready)
        self.assertEqual(unbound.binding_state, "unimplemented")
        self.assertIsNone(unbound.runtime_provider)

    def test_paused_unknown_and_unapproved_all_are_rejected(self) -> None:
        paused = build_plan("https://www.tiktok.com/@example/video/1")
        self.assertFalse(paused.ok)
        self.assertEqual(paused.error_code, "capability_paused")

        unknown = build_plan("C:\\fixture\\unknown.bin", input_kind="file")
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error_code, "source_unsupported")

        unapproved_all = build_plan("https://example.com/article", request_all=True)
        self.assertFalse(unapproved_all.ok)
        self.assertEqual(unapproved_all.error_code, "all_not_approved")


class CliProjectionTests(unittest.TestCase):
    def test_capabilities_and_plan_json_are_stable(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli_main(["capabilities", "--source", "tiktok", "--format", "json"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["capabilities"][0]["state"], "paused")
        self.assertFalse(payload["capabilities"][0]["execution_ready"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli_main(["plan", "https://example.com/article", "--format", "json"]), 0)
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["detected_source"], "web_article")
        self.assertFalse(plan["source_exhausted_requested"])
        self.assertFalse(plan["execution_ready"])

    def test_doctor_profiles_are_safe_and_redacted(self) -> None:
        for profile in ("core", "web", "media", "documents", "authenticated-social"):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(cli_main(["doctor", "--profile", profile]), 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["profile"], profile)
            self.assertFalse(payload["safe_session_boundary"]["reads_private_browser_profiles"])
            self.assertEqual(payload["profile_diagnostics"]["private_session_state"], "not_read")


if __name__ == "__main__":
    unittest.main()
