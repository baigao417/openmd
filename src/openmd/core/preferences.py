"""Nonsecret preferences for the interactive single-link workflow."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


SCHEMA = "openmd.preferences/v1"


def data_directory() -> Path:
    configured = os.environ.get("OPENMD_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".openmd"


def preferences_path() -> Path:
    return data_directory() / "preferences.json"


def load_preferences() -> dict[str, object]:
    """Malformed preferences never grant permissions or block extraction."""
    path = preferences_path()
    try:
        if not path.is_file() or path.stat().st_size > 16 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return {}
    result: dict[str, object] = {}
    directory = value.get("last_save_directory")
    if isinstance(directory, str) and directory.strip():
        result["last_save_directory"] = directory
    if type(value.get("browser_always")) is bool:
        result["browser_always"] = value["browser_always"]
    return result


def save_preferences(*, last_save_directory: Path | None = None, browser_always: bool | None = None) -> None:
    """Only these explicit nonsecret values can enter the preference file."""
    value = {"schema": SCHEMA, **load_preferences()}
    if last_save_directory is not None:
        value["last_save_directory"] = str(last_save_directory.expanduser().resolve())
    if browser_always is not None:
        value["browser_always"] = bool(browser_always)
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
