from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "transcribe_audio.py"
SPEC = importlib.util.spec_from_file_location("openmd_transcribe_audio", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_format_timestamp_handles_long_audio() -> None:
    assert MODULE.format_timestamp(7740.9) == "[02:09:00]"


def test_clean_text_collapses_whitespace_without_rewriting_words() -> None:
    assert MODULE.clean_text("  搞钱女孩\u3000 AI   创业 ") == "搞钱女孩 AI 创业"


def test_cpu_runtime_defaults_to_int8() -> None:
    assert MODULE.choose_runtime("cpu", "auto") == ("cpu", "int8")
