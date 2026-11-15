"""Console entry points must work with redirected Windows encodings."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii", "utf-8"])
@pytest.mark.parametrize("args", [["--help"], ["doctor", "--profile", "core", "--json"], ["doctor", "--profile", "core"], ["go", "bad://example", "-o", "unused"]])
def test_cli_stream_encodings(encoding, args, monkeypatch, tmp_path):
    from openmd.cli import main
    from openmd.runtime import managed_tools, tempdirs

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(managed_tools, "activate", lambda: "missing")
    monkeypatch.setattr(tempdirs, "sweep_at_startup", lambda: None)
    out, err = io.BytesIO(), io.BytesIO()
    class Terminal(io.TextIOWrapper):
        def isatty(self):
            return args[0] == "doctor" and "--json" not in args

    stdout = Terminal(out, encoding=encoding, write_through=True)
    stderr = io.TextIOWrapper(err, encoding=encoding, write_through=True)
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stdout)
        streams.setattr(sys, "stderr", stderr)
        code = main(args)
    output = out.getvalue().decode(encoding)
    errors = err.getvalue().decode(encoding)
    if args[0] == "doctor":
        assert code == 0
        if "--json" in args:
            assert json.loads(output)["command"] == "doctor"
        else:
            assert "Openmd" in output
    elif args[0] == "go":
        assert code == 2 and errors
    else:
        assert code == 0 and "openmd" in output
    assert "UnicodeEncodeError" not in output + errors


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii", "utf-8"])
def test_dev_setup_check_stream_encoding(encoding):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/dev_setup.py"), "--check"],
        capture_output=True, env={**os.environ, "PYTHONIOENCODING": encoding},
        timeout=30,
    )
    assert result.returncode in (0, 1)
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"python -m pytest" in result.stdout
    if encoding in {"cp936", "utf-8"}:
        assert "下一步" in result.stdout.decode(encoding)


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii", "utf-8"])
def test_console_preserves_supported_text_and_degrades_symbols(encoding, monkeypatch):
    from openmd.console import configure_console

    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding=encoding, write_through=True)
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stream)
        configure_console()
        print("中文 ✓ · ✗ → 😀")
    value = data.getvalue().decode(encoding)
    assert "?" not in value
    if encoding in {"cp936", "utf-8"}:
        assert "中文" in value
    if encoding != "utf-8":
        assert "[OK]" in value and "[X]" in value
    assert stream.encoding == encoding


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii"])
def test_json_save_receipt_roundtrips_unicode(encoding, monkeypatch, tmp_path):
    import argparse
    from openmd.console import configure_console
    from openmd.interactive import _emit_saved

    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding=encoding, write_through=True)
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stream)
        configure_console()
        _emit_saved(argparse.Namespace(json=True), target=tmp_path / "中文✓😀.md", outputs=[], records=None)
    assert json.loads(data.getvalue().decode(encoding))["saved_to"] == str(tmp_path / "中文✓😀.md")
