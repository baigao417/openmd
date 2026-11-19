"""Diagnostics artifacts that are safe to attach to a bug report."""

from .sanitize import sanitize_snapshot, sanitize_url

__all__ = ["sanitize_snapshot", "sanitize_url"]
