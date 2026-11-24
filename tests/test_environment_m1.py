from __future__ import annotations

from argparse import Namespace

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import BackendExecutor
from openmd.core.environment import is_headless, is_non_interactive
from openmd.core.models import ContentItem


def test_headless_override_is_explicit_and_reversible(monkeypatch):
    monkeypatch.setenv("OPENMD_BROWSER_MODE", "headless")
    assert is_headless()
    monkeypatch.setenv("OPENMD_BROWSER_MODE", "desktop")
    assert not is_headless()


def test_json_args_are_non_interactive_even_with_a_tty(monkeypatch):
    monkeypatch.delenv("OPENMD_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert is_non_interactive(Namespace(json=True, yes=False, command="go"))


def test_headless_executor_skips_browser_provider_and_records_attempt():
    class ForbiddenProvider:
        def extract(self, request):
            raise AssertionError("browser provider must not be called in headless mode")

    item = ContentItem(platform="x", input_type="single", url="https://x.com/a/status/1")
    result = BackendExecutor(
        providers={"openmd.x.logged_in_browser": ForbiddenProvider()},
        headless=True,
    ).extract(item, ("openmd.x.logged_in_browser",))
    assert result.error_code == "headless_browser_unavailable"
    assert result.metadata["fallback_attempts"] == [{
        "backend": "openmd.x.logged_in_browser",
        "error_code": "headless_browser_unavailable",
    }]
