"""Small logging sanitizers for secrets and oversized raw artifacts."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ALLOWED_RUNLOG_FIELDS: frozenset[str] = frozenset(
    {
        "input_url",
        "platform",
        "backend",
        "status",
        "source_type",
        "source_level",
        "output_path",
        "package_id",
        "package_operation",
        "started_at",
        "finished_at",
        "item_count",
        "success_count",
        "failed_count",
        "event_type",
        "failure_class",
        "guardrail_signals",
        "resume_pointer",
        "error_code",
        "error_summary",
        "extraction_contract_version",
        "components",
        "terminal_gap_count",
    }
)

SENSITIVE_KEY_TERMS: tuple[str, ...] = (
    "cookie",
    "token",
    "authorization",
    "set-cookie",
    "bearer",
    "header",
    "api_response",
    "response_body",
    "raw_html",
    "html_dump",
    "raw_subtitle",
    "subtitle_body",
    "raw_transcript",
    "raw_media",
    "media_path",
    "stdout",
    "stderr",
    "xsec_token",
)

SENSITIVE_QUERY_KEY_TERMS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "code",
    "authorization",
    "auth",
    "cookie",
    "secret",
    "signature",
    "sig",
    "credential",
    "session",
    "xsec_token",
)

SENSITIVE_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?is)<html\b.*?</html>"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^,;\s]+"),
    re.compile(
        r"(?i)\b(?:set-cookie|cookie|authorization|bearer|token|headers?)\b"
        r"\s*[:=]\s*[^,;\s]+"
    ),
    re.compile(
        r"(?i)\b(?:full api response|api response|raw transcript|raw subtitles?|"
        r"raw html|html dump|media path|raw media|stdout|stderr)\b\s*[:=]\s*[^,;\n]*"
    ),
)

URL_RE = re.compile(r"https?://[^\s<>'\")\]]+")


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("_", "-")
    return any(term.replace("_", "-") in normalized for term in SENSITIVE_KEY_TERMS)


def is_sensitive_query_key(key: str) -> bool:
    normalized = key.lower().replace("_", "-")
    return any(
        term.replace("_", "-") in normalized for term in SENSITIVE_QUERY_KEY_TERMS
    )


def _sanitize_fragment(fragment: str) -> str:
    if not fragment or "=" not in fragment:
        return fragment
    pairs = parse_qsl(fragment, keep_blank_values=True)
    if not pairs:
        return fragment
    safe_pairs = [(k, v) for k, v in pairs if not is_sensitive_query_key(k)]
    return urlencode(safe_pairs)


def sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    if not parsed.query and not parsed.fragment:
        return value

    safe_params = [
        (key, param_value)
        for key, param_value in parse_qsl(parsed.query, keep_blank_values=True)
        if not is_sensitive_query_key(key)
    ]
    safe_fragment = _sanitize_fragment(parsed.fragment)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(safe_params), safe_fragment)
    )


def sanitize_url_text(value: str) -> str:
    return URL_RE.sub(lambda match: sanitize_url(match.group(0)), value)


def truncate_text(value: str, *, max_length: int = 240) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 14].rstrip() + " ...[truncated]"


def sanitize_error_summary(value: Any, *, max_length: int = 240) -> str | None:
    if value is None:
        return None

    text = sanitize_url_text(str(value).replace("\r", " ").replace("\n", " "))
    for pattern in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub("[redacted]", text)

    # Remove bare sensitive words too, so summaries do not leak field names.
    for term in (
        "cookie",
        "token",
        "authorization",
        "set-cookie",
        "bearer",
        "header",
        "headers",
        "xsec_token",
    ):
        text = re.sub(rf"(?i)\b{re.escape(term)}\b", "[redacted]", text)

    return truncate_text(" ".join(text.split()), max_length=max_length)


def sanitize_for_logging(value: Any, *, key: str | None = None) -> Any:
    if key and is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            if is_sensitive_key(child_key_text):
                continue
            sanitized[child_key_text] = sanitize_for_logging(
                child_value, key=child_key_text
            )
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_logging(item, key=key) for item in value[:20]]
    if isinstance(value, tuple):
        return [sanitize_for_logging(item, key=key) for item in value[:20]]
    if isinstance(value, str):
        if key in {"warnings", "terminal_reason"}:
            return sanitize_error_summary(value)
        return truncate_text(sanitize_url_text(value))
    return value


def sanitize_runlog_entry(entry: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in ALLOWED_RUNLOG_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if key in {"error_code", "error_summary"}:
            sanitized[key] = sanitize_error_summary(value)
        elif isinstance(value, (dict, list, tuple)):
            sanitized[key] = sanitize_for_logging(value, key=key)
        elif isinstance(value, str):
            sanitized[key] = truncate_text(sanitize_url_text(value))
        else:
            sanitized[key] = value
    return sanitized
