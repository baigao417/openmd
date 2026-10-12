"""Shared high-privacy export/cache status helpers.

High-privacy social/chat lanes must report operator-actionable states without
claiming ownership of platform sessions, browser profiles, databases, cookies,
or decrypted local stores.
"""

from __future__ import annotations

from typing import Any

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


HIGH_PRIVACY_STATUS_KEY = "high_privacy_status"


def high_privacy_status_metadata(
    *,
    platform: str,
    target_kind: str,
    state: str,
    next_action: str,
    permission_scope: str | None = None,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "target_kind": target_kind,
        "state": state,
        "permission_scope": permission_scope or "unknown",
        "next_action": sanitize_error_summary(next_action),
        "core_owns_session": False,
        "reads_private_database": False,
        "decrypts_database": False,
        "exports_cookies": False,
        "persists_private_cache": False,
        "requires_explicit_scope": True,
    }


def high_privacy_blocker_result(
    *,
    platform: str,
    backend: str,
    target_kind: str,
    state: str,
    error_code: str,
    error_summary: str,
    next_action: str,
    permission_scope: str | None = None,
    title: str | None = None,
    source_notes: list[str] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        title=title,
        source_notes=[
            *(source_notes or []),
            "Openmd core did not read private databases, decrypt stores, export cookies, or own platform sessions.",
        ],
        error_code=error_code,
        error_summary=sanitize_error_summary(error_summary),
        metadata={
            "failure_class": "user_resolvable",
            HIGH_PRIVACY_STATUS_KEY: high_privacy_status_metadata(
                platform=platform,
                target_kind=target_kind,
                state=state,
                next_action=next_action,
                permission_scope=permission_scope,
            ),
        },
    )
