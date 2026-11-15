from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ASR = load_script("douyin_asr_missing_transcripts.py")
OCR = load_script("douyin_ocr_transcripts.py")


def test_asr_helper_uses_current_bounded_opencli_contract() -> None:
    with patch.object(ASR, "command_path", return_value="opencli"):
        command = ASR.build_opencli_user_videos_command(
            "fixture-sec-uid",
            site_session="persistent",
            limit=999,
        )

    assert command == [
        "opencli",
        "douyin",
        "user-videos",
        "fixture-sec-uid",
        "--limit",
        "20",
        "--with_comments",
        "false",
        "--site-session",
        "persistent",
        "-f",
        "json",
    ]
    assert "--all" not in command
    assert "video-detail" not in command


def test_ocr_helper_rejects_unsupported_full_discovery_before_running_opencli() -> None:
    with patch.object(OCR, "_command_path", side_effect=AssertionError("must fail before command lookup")):
        with pytest.raises(RuntimeError, match="supports only --limit"):
            OCR.fetch_opencli_rows(
                "fixture-sec-uid",
                limit=20,
                all_items=True,
                site_session="persistent",
                timeout=30,
            )
