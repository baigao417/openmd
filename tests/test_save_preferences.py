"""Automation must preserve a human's default destination byte for byte."""
import pytest

from openmd import interactive
from openmd.cli import main
from openmd.core.preferences import load_preferences, preferences_path, save_preferences


@pytest.mark.parametrize("tty,flags", [
    (False, []), (False, ["--json"]), (True, ["--json"]),
    (True, ["--yes"]), (True, ["--yes", "--json"]),
])
def test_noninteractive_go_preserves_preferences_bytes_and_mtime(tmp_path, monkeypatch, capsys, tty, flags):
    monkeypatch.setattr(interactive, "is_interactive", lambda: tty)
    save_preferences(last_save_directory=tmp_path / "human-default", browser_always=True)
    path = preferences_path()
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert main(["go", "https://example.org/article", "--backend", "mock", "-o", str(tmp_path / "agent-output"), *flags]) == 0
    assert list((tmp_path / "agent-output").glob("*.md"))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    capsys.readouterr()


@pytest.mark.parametrize("flags", [["--json"], ["--yes"]])
def test_automation_does_not_create_preferences(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    assert not preferences_path().exists()
    assert main(["go", "https://example.org/article", "--backend", "mock", "-o", str(tmp_path / "output"), *flags]) == 0
    assert not preferences_path().exists()


@pytest.mark.parametrize("prompt", [True, False])
def test_interactive_save_still_remembers_destination(tmp_path, monkeypatch, prompt):
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    destination = tmp_path / "human-output"
    monkeypatch.setattr("builtins.input", lambda message: str(destination))
    flags = [] if prompt else ["-o", str(destination)]
    assert main(["go", "https://example.org/article", "--backend", "mock", *flags]) == 0
    assert load_preferences()["last_save_directory"] == str(destination.resolve())


def test_batch_preserves_preferences(tmp_path, capsys):
    save_preferences(last_save_directory=tmp_path / "human-default")
    path = preferences_path()
    before = path.read_bytes(), path.stat().st_mtime_ns
    links = tmp_path / "links.txt"
    links.write_text("https://example.org/article\n", encoding="utf-8")
    assert main(["batch", str(links), "--backend", "mock", "--out", str(tmp_path / "batch-output")]) == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    capsys.readouterr()
