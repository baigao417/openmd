"""Execution environment guards shared by CLI and backend routing."""

from __future__ import annotations

import os
import sys
from typing import Any


def is_headless() -> bool:
    """Return whether browser based routes cannot be used in this process.

    ``OPENMD_BROWSER_MODE`` is intentionally a test/diagnostic override, not a
    daily user setting.  Linux and macOS processes without a display session
    are treated as headless; Windows keeps the existing desktop behavior.
    """
    override = os.environ.get("OPENMD_BROWSER_MODE", "").strip().lower()
    if override == "headless":
        return True
    if override == "desktop":
        return False
    if sys.platform.startswith(("linux", "darwin")):
        return not bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def is_non_interactive(args: Any = None) -> bool:
    """Return whether prompts, GUI and long login waits are forbidden."""
    if os.environ.get("OPENMD_NON_INTERACTIVE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if not (getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)()):
        return True
    if args is None:
        return False
    if getattr(args, "json", False) or getattr(args, "yes", False):
        return True
    command = getattr(args, "command", None)
    if command in {"batch", "mcp"} or getattr(args, "batch", False) or getattr(args, "mcp", False):
        return True
    return False
