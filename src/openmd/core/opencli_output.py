"""Single boundary for OpenCLI's machine-readable stdout.

OpenCLI 2.0 wraps JSON output as ``{"ok": true, "data": ...}`` and failures as
``{"ok": false, "error": {...}}``. Provider parsers should only ever see the
inner rows, so the envelope is removed here and nowhere else. Older bare JSON
passes through unchanged, which keeps OpenCLI 1.x output and fixtures readable.
"""

from __future__ import annotations

import json
from typing import Any

_ENVELOPE_KEYS = frozenset({"ok", "data", "error"})


def is_opencli_envelope(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("ok"), bool)
        and _ENVELOPE_KEYS.issuperset(payload)
    )


def unwrap_opencli_envelope(payload: Any) -> Any:
    """Return successful envelope data; failures stay intact for error handling."""
    if is_opencli_envelope(payload) and payload["ok"] and "data" in payload:
        return payload["data"]
    return payload


def load_opencli_json(stdout: str) -> Any:
    """``json.loads`` for OpenCLI stdout; raises ``JSONDecodeError`` the same way."""
    return unwrap_opencli_envelope(json.loads(stdout))
