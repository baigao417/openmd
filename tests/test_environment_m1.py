from __future__ import annotations

from argparse import Namespace
from pathlib import Path

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
    monkeypatch.setenv("OPENMD_NON_INTERACTIVE", "0")  # registered so monkeypatch restores it after cli.main sets it
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


def test_cli_marks_json_runs_non_interactive_for_the_whole_process(monkeypatch, tmp_path):
    """Round16 Muse: an agent in a pseudo-terminal with --json must not reach TTY-only prompts
    (Xiaoyuzhou QR wait, OCR first use, model install confirmation)."""
    import os
    from openmd import cli
    from openmd.interactive import is_interactive

    monkeypatch.setenv("OPENMD_NON_INTERACTIVE", "0")  # registered so monkeypatch restores it after cli.main sets it
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    seen = {}

    def fake_func(args):
        seen["interactive"] = is_interactive()
        seen["env"] = os.environ.get("OPENMD_NON_INTERACTIVE")
        return 0

    parser = cli.build_parser()
    real_parse = parser.parse_args

    def parse(argv):
        args = real_parse(argv)
        args.func = fake_func
        return args

    monkeypatch.setattr(cli, "build_parser", lambda: type("P", (), {"parse_args": staticmethod(parse)})())
    assert cli.main(["go", "https://example.org/a", "-o", str(tmp_path), "--json"]) == 0
    assert seen == {"interactive": False, "env": "1"}


def test_gui_confirm_callback_still_asks_without_a_terminal():
    """The right-click GUI has no terminal but its own dialog; that dialog must keep working."""

    source = (Path(__file__).resolve().parents[1] / "src" / "openmd" / "cli_items.py").read_text(encoding="utf-8")
    assert 'callable(first_use_confirm) and not getattr(args, "json", False)' in source


def test_missing_ocr_backend_tells_agents_what_to_install():
    from openmd.cli_login import source_next_action

    assert "openmd models install ocr:chain" in source_next_action("ocr_backend_missing")
    source = (Path(__file__).resolve().parents[1] / "src" / "openmd" / "cli_items.py").read_text(encoding="utf-8")
    assert '"id": "ocr:chain"' in source


def test_non_interactive_mark_does_not_outlive_one_cli_call(monkeypatch, tmp_path):
    import os
    from openmd import cli

    monkeypatch.delenv("OPENMD_NON_INTERACTIVE", raising=False)
    cli.main(["go", "https://example.org/a", "--backend", "mock", "-o", str(tmp_path), "--json"])
    assert "OPENMD_NON_INTERACTIVE" not in os.environ
