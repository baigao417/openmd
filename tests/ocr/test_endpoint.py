"""Loopback boundary regressions; no network requests or model loads."""

from __future__ import annotations

import os
import socket
import time
import unittest
from unittest.mock import patch

from openmd.ocr.config import settings
from openmd.ocr.endpoint import EndpointError, validate_ollama_url


def answers(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 18499)) for address in addresses]


class EndpointTests(unittest.TestCase):
    def test_loopback_literals_are_accepted_without_dns(self):
        for url in ("http://127.0.0.1:18499", "http://127.2.3.4:11434", "http://[::1]:18499", "https://[0:0:0:0:0:0:0:1]:443"):
            with self.subTest(url=url), patch("openmd.ocr.endpoint.socket.getaddrinfo") as resolve:
                result = validate_ollama_url(url)
                self.assertIn("127." if "127." in url else "[::1]", result)
                resolve.assert_not_called()

    def test_hostname_all_answers_must_be_loopback_and_connection_is_pinned(self):
        for host in ("localhost", "LOCALHOST", "localhost.", "my-local-ollama.example"):
            with self.subTest(host=host), patch("openmd.ocr.endpoint.socket.getaddrinfo", return_value=answers("127.0.0.1", "::1")):
                self.assertEqual(validate_ollama_url(f"http://{host}:18499/prefix/"), "http://127.0.0.1:18499/prefix")
        with patch("openmd.ocr.endpoint.socket.getaddrinfo", return_value=answers("::1", "127.0.0.1")):
            self.assertEqual(validate_ollama_url("http://localhost:18499"), "http://[::1]:18499")

    def test_remote_and_alternate_ip_forms_are_rejected_without_dns(self):
        hosts = ("198.51.100.7", "0.0.0.0", "169.254.169.254", "[::]", "[2001:db8::1]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[::127.0.0.1]", "2130706433", "0177.0.0.1", "127.000.000.001", "127.1", "0x7f000001", "0x7f.0.0.1", "127.0.0.1.", "4294967295")
        for host in hosts:
            with self.subTest(host=host), patch("openmd.ocr.endpoint.socket.getaddrinfo") as resolve:
                with self.assertRaises(EndpointError) as caught:
                    validate_ollama_url(f"http://{host}:18499")
                self.assertEqual(caught.exception.code, "ocr_ollama_url_not_loopback")
                resolve.assert_not_called()

    def test_hostname_prefix_and_mixed_dns_cannot_bypass_boundary(self):
        for host, records in (("localhost.evil.com", answers("198.51.100.7")), ("localhost", answers("127.0.0.1", "198.51.100.7")), ("localhost", answers("::1", "::ffff:127.0.0.1")), ("evil.example", answers("198.51.100.7", "127.0.0.1"))):
            with self.subTest(host=host, records=records), patch("openmd.ocr.endpoint.socket.getaddrinfo", return_value=records):
                with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_not_loopback"):
                    validate_ollama_url(f"http://{host}:18499")

    def test_resolution_failure_and_no_answers_fail_closed(self):
        with patch("openmd.ocr.endpoint.socket.getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_resolution_failed"):
                validate_ollama_url("http://localhost:18499")
        with patch("openmd.ocr.endpoint.socket.getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_resolution_failed"):
                validate_ollama_url("http://localhost:18499")

    def test_malformed_authority_and_encoded_hosts_rejected(self):
        urls = ("http://user@127.0.0.1:18499", "http://127.0.0.1:18499?target=evil", "http://127.0.0.1#evil", "file:///127.0.0.1", "http://127.0.0.1:65536", "http://127.0.0.1:0", "http://127.0.0.1:notaport", "http://127.0.0.1\n", "http://127.0.0.1\\@evil.example", "http://127%2e0%2e0%2e1", "http://[::1%25lo]:18499")
        for url in urls:
            with self.subTest(url=url), self.assertRaisesRegex(EndpointError, "ocr_ollama_url_invalid"):
                validate_ollama_url(url)

    def test_settings_environment_and_local_config_share_validation(self):
        with patch.dict(os.environ, {"OPENMD_OLLAMA_URL": "http://198.51.100.7:18499"}), patch("openmd.ocr.config.load_local_config", return_value={}):
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_not_loopback"):
                settings()
        with patch.dict(os.environ, {"OPENMD_OLLAMA_URL": ""}), patch("openmd.ocr.config.load_local_config", return_value={"ollama_url": "http://198.51.100.7:18499"}):
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_not_loopback"):
                settings()

    def test_direct_api_call_rejects_remote_before_building_request(self):
        from openmd.ocr.ollama import api_json

        with patch("openmd.ocr.ollama.urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_not_loopback"):
                api_json("http://198.51.100.7:18499", "/api/chat", {"images": ["synthetic"]})
            opener.assert_not_called()

    def test_direct_stream_call_rejects_remote_before_connecting(self):
        from PIL import Image
        from openmd.ocr.ollama import stream_recognize

        config = {**settings(), "ollama_url": "http://198.51.100.7:18499"}
        with patch("openmd.ocr.ollama.http.client.HTTPConnection") as connection:
            with self.assertRaisesRegex(EndpointError, "ocr_ollama_url_not_loopback"):
                stream_recognize(Image.new("RGB", (40, 40)), model="fixture", prompt="raw OCR", config=config, deadline=time.monotonic() + 1)
            connection.assert_not_called()

    def test_local_redirect_never_connects_to_location(self):
        from email.message import Message
        from openmd.ocr.ollama import OllamaError, api_json

        for status in (302, 307, 308):
            def fake_open(request, timeout):
                headers = Message()
                headers["Location"] = "http://198.51.100.7:18499/api/chat"
                return handler.redirect_request(request, None, status, "redirect", headers, headers["Location"])
            with self.subTest(status=status), patch("openmd.ocr.ollama.urllib.request.build_opener") as build:
                opener = build.return_value
                opener.open.side_effect = fake_open
                # Capture the actual installed handler; no transport runs.
                def make_opener(*handlers):
                    nonlocal handler
                    handler = next(x for x in handlers if hasattr(x, "redirect_request"))
                    return opener
                handler = None
                build.side_effect = make_opener
                with self.assertRaisesRegex(OllamaError, "ocr_ollama_redirect_rejected"):
                    api_json("http://127.0.0.1:18499", "/api/tags")
                self.assertEqual(opener.open.call_count, 1)
