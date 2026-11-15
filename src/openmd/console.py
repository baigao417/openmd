"""Keep the caller's console encoding; degrade only unsupported characters."""
from __future__ import annotations

import codecs
import sys


_SYMBOLS = {"✓": "[OK]", "✔": "[OK]", "✗": "[X]", "✘": "[X]", "·": "-", "→": "->", "…": "..."}


def _encoding_fallback(error: UnicodeEncodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeEncodeError):
        raise error
    return "".join(
        _SYMBOLS.get(char, char.encode("ascii", "backslashreplace").decode("ascii"))
        for char in error.object[error.start:error.end]
    ), error.end


def configure_console() -> None:
    """Also covers argparse and errors written directly to stdout/stderr.

    Chinese stays Chinese on cp936. Unsupported text uses visible escapes,
    while common status symbols get ASCII words. JSON callers use ensure_ascii
    so neither those substitutions nor non-BMP escapes alter JSON values.
    In-memory capture streams without reconfigure need no encoding policy.
    """
    codecs.register_error("openmd_console", _encoding_fallback)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="openmd_console")
