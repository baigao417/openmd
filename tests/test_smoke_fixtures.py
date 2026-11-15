from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from openmd.core.detector import detect_platform


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "fixtures"
    / "platform-smoke-fixtures.json"
)

V0_CATEGORY_IDS = [
    "bilibili",
    "youtube",
    "weixin",
    "xiaoyuzhou",
    "douyin_tiktok",
    "xiaohongshu",
    "xiaoe",
    "generic_media",
    "x",
    "web_article",
    "feishu",
]

EXPECTED_OUTCOMES = {"verified", "partial", "expected_auth_failure"}
SAMPLE_KINDS = {"public_url", "auth_placeholder", "user_input_placeholder"}
CI_POLICIES = {
    "skip_unless_network_enabled",
    "skip_unless_user_provided_local_auth",
    "skip_missing_user_sample",
}
FORBIDDEN_QUERY_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "course_id",
    "product_id",
    "refresh_token",
    "session",
    "sub_course_id",
    "token",
    "vd_source",
    "xsec_source",
    "xsec_token",
}


class PlatformSmokeFixtureManifestTests(unittest.TestCase):
    def _load_manifest(self) -> dict[str, object]:
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_manifest_covers_exact_v0_categories(self) -> None:
        manifest = self._load_manifest()
        fixtures = manifest["fixtures"]

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual([entry["category_id"] for entry in fixtures], V0_CATEGORY_IDS)
        self.assertEqual(len(fixtures), 11)

        for entry in fixtures:
            with self.subTest(category=entry["category_id"]):
                self.assertTrue(entry["platforms"])
                self.assertTrue(entry["samples"])

    def test_samples_are_safe_and_match_detector(self) -> None:
        fixtures = self._load_manifest()["fixtures"]
        seen_sample_ids: set[str] = set()

        for entry in fixtures:
            platforms = set(entry["platforms"])
            for sample in entry["samples"]:
                with self.subTest(sample=sample["id"]):
                    self.assertNotIn(sample["id"], seen_sample_ids)
                    seen_sample_ids.add(sample["id"])

                    self.assertIn(sample["platform"], platforms)
                    self.assertIn(sample["sample_kind"], SAMPLE_KINDS)
                    self.assertIn(sample["expected_outcome"], EXPECTED_OUTCOMES)
                    self.assertIn(sample["ci_policy"], CI_POLICIES)
                    self.assertIsInstance(sample["requires_network"], bool)
                    self.assertIsInstance(sample["requires_auth"], bool)

                    if sample["requires_auth"]:
                        self.assertEqual(sample["sample_kind"], "auth_placeholder")
                        self.assertEqual(sample["expected_outcome"], "expected_auth_failure")
                        self.assertEqual(
                            sample["ci_policy"],
                            "skip_unless_user_provided_local_auth",
                        )

                    url = sample["url"]
                    if url is None:
                        self.assertEqual(sample["sample_kind"], "user_input_placeholder")
                        self.assertEqual(sample["ci_policy"], "skip_missing_user_sample")
                        continue

                    self.assertEqual(detect_platform(url), sample["platform"])
                    self._assert_url_has_no_forbidden_query(url)

    def test_manifest_does_not_store_private_payload_fields(self) -> None:
        fixtures = self._load_manifest()["fixtures"]

        for entry in fixtures:
            for sample in entry["samples"]:
                with self.subTest(sample=sample["id"]):
                    self.assertNotIn("headers", sample)
                    self.assertNotIn("cookies", sample)
                    self.assertNotIn("raw_html", sample)
                    self.assertNotIn("raw_transcript", sample)
                    self.assertNotIn("api_response", sample)

    def _assert_url_has_no_forbidden_query(self, url: str) -> None:
        parsed = urlparse(url)
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            self.assertNotIn(key.lower(), FORBIDDEN_QUERY_KEYS)


if __name__ == "__main__":
    unittest.main()
