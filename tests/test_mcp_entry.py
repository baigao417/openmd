from __future__ import annotations

import builtins
from pathlib import Path

from openmd.cli import main


def test_mcp_missing_extra_is_actionable_without_stdout(monkeypatch, capsys):
    original = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    assert main(["mcp"]) == 2
    output = capsys.readouterr()
    assert 'pip install "openmd-cli[mcp]"' in output.err
    assert not output.out


def test_mcp_is_an_optional_pinned_dependency():
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'mcp = ["mcp==1.26.0"]' in project.read_text(encoding="utf-8")


def test_mcp_parser_needs_no_sdk():
    from openmd.cli import build_parser

    assert build_parser().parse_args(["mcp"]).command == "mcp"
