"""Small logging sanitizers for secrets and oversized raw artifacts."""

from __future__ import annotations

import math
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
        "route_trace",
        "document_metadata",
        "asr_execution",
        "source_scope",
        "completeness",
        "sync",
        "login",
        "original_error_code",
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
        elif key == "input_url" and isinstance(value, str):
            # This locator is hashed again by compact receipts. Truncating it
            # creates a second identity for the same discovered item.
            sanitized[key] = sanitize_url(value)
        elif key == "document_metadata":
            sanitized[key] = sanitize_document_metadata(value)
        elif key == "asr_execution":
            sanitized[key] = sanitize_asr_execution(value)
        elif key == "source_scope":
            sanitized[key] = sanitize_source_scope(value)
        elif isinstance(value, (dict, list, tuple)):
            sanitized[key] = sanitize_for_logging(value, key=key)
        elif isinstance(value, str):
            sanitized[key] = truncate_text(sanitize_url_text(value))
        else:
            sanitized[key] = value
    return sanitized


def sanitize_asr_execution(value: Any) -> dict[str, Any]:
    """Publish numeric ASR metrics and bounded route labels, never source text."""
    if not isinstance(value, dict):
        return {}

    def number(v: Any) -> bool:
        return type(v) in (int, float) and math.isfinite(v) and v >= 0

    keys = {"elapsed_seconds", "inference_seconds", "batch_size_requested", "batch_size_effective",
            "batch_oom_retries", "torch_peak_allocated_bytes", "torch_peak_reserved_bytes",
            "memory_available_bytes", "memory_required_bytes", "memory_estimated_peak_bytes", "memory_margin_bytes",
            "memory_warning_threshold_bytes"}
    result = {key: value[key] for key in keys if key in value and number(value[key])}
    if value.get("memory_warning") == "asr_memory_tight":
        result["memory_warning"] = "asr_memory_tight"
    probe = value.get("language_probe_memory")
    if isinstance(probe, dict):
        safe_probe = sanitize_asr_execution({key: probe[key] for key in (*keys, "memory_warning") if key in probe and key.startswith("memory_")})
        if safe_probe:
            result["language_probe_memory"] = safe_probe
    if value.get("model_origin") in {"prepared", "original"}:
        result["model_origin"] = value["model_origin"]
    if type(value.get("prepare_recommended")) is bool:
        result["prepare_recommended"] = value["prepare_recommended"]
        if value["prepare_recommended"]:
            result["prepare_command"] = "openmd asr prepare-models"
    stages = value.get("stage_seconds")
    if isinstance(stages, dict):
        result["stage_seconds"] = {key: stages[key] for key in ("media_discovery", "download_audio", "asr") if key in stages and number(stages[key])}
    if number(value.get("navigation_seconds")):
        result["navigation_seconds"] = value["navigation_seconds"]
    for key in ("duration", "segment_count", "coverage_end_seconds"):
        if key in value and number(value[key]):
            result[key] = value[key]
    for key in ("model", "model_size", "device", "language", "detected_language"):
        label = value.get(key)
        if isinstance(label, str) and (re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", label)
                                       or (key == "model" and label == "Qwen/Qwen3-ASR-1.7B")):
            result[key] = label
    backend = value.get("asr_backend")
    if backend in {"asr.auto", "asr.qwen3", "asr.faster_whisper", "asr.tingwu"}:
        result["asr_backend"] = backend
    selection = value.get('asr_selection')
    if isinstance(selection, dict):
        selected = selection.get('selected_backend')
        reason = selection.get('reason')
        if selected in {'asr.auto', 'asr.tingwu'} and reason in {'cloud_not_authorized', 'duration_unknown', 'below_threshold', 'explicit_cloud_gate', 'local_long_course_opt_in'}:
            result['asr_selection'] = {'selected_backend': selected, 'reason': reason}
            for key in ('duration_seconds', 'threshold_seconds'):
                if number(selection.get(key)):
                    result['asr_selection'][key] = selection[key]
    transport = value.get('media_transport')
    if isinstance(transport, dict) and transport.get('method') in {'hls_prefetch', 'ffmpeg'}:
        result['media_transport'] = {'method':transport['method']}
        for key in ('segments', 'concurrency', 'network_bytes', 'network_seconds', 'decode_seconds'):
            if number(transport.get(key)):
                result['media_transport'][key] = transport[key]
    phases = value.get('cloud_phase_seconds')
    if isinstance(phases, dict):
        result['cloud_phase_seconds'] = {key:phases[key] for key in ('navigate','open_upload','upload_file','submit','poll') if number(phases.get(key))}
    route = value.get("asr_route")
    if isinstance(route, dict):
        result.update(sanitize_asr_execution({
            "asr_backend": route.get("selected_backend"),
            "detected_language": route.get("detected_language"),
        }))
    histogram = value.get("batch_size_histogram")
    if isinstance(histogram, dict):
        result["batch_size_histogram"] = {str(n): histogram[str(n)] for n in range(1, 33)
                                          if str(n) in histogram and type(histogram[str(n)]) is int and histogram[str(n)] >= 0}
    attempts = value.get("batch_oom_attempts")
    if isinstance(attempts, list):
        result["batch_oom_attempts"] = [v for v in attempts[:6] if type(v) is int and 1 <= v <= 32]
    return result


def asr_execution_metrics(metadata: dict[str, Any]) -> dict[str, Any]:
    worker = metadata.get("asr_execution")
    return sanitize_asr_execution({**metadata, **(worker if isinstance(worker, dict) else {})})


def sanitize_source_scope(value: Any) -> dict[str, Any]:
    """A small structural receipt; no text, signed URLs or arbitrary API data."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("engine", "scope"):
        label = value.get(key)
        if isinstance(label, str) and re.fullmatch(r"[a-z_]{1,40}", label):
            result[key] = label
    for key in ("topic_id", "post_id", "target_post_id"):
        identity = value.get(key)
        if type(identity) is int and identity > 0:
            result[key] = identity
        elif isinstance(identity, str) and re.fullmatch(r"\d{1,24}", identity):
            result[key] = identity
    if value.get("post_body_present") is True:
        result["post_body_present"] = True
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        safe: dict[str, Any] = {}
        for key, metric in coverage.items():
            if key not in {"duration_seconds", "expected_count", "fetched_count", "missing_count", "visible_count",
                           "expected_posts", "fetched_posts", "missing_posts", "initial_count", "request_count",
                           "sample_interval_seconds", "max_seconds", "sampled_seconds", "complete"}:
                continue
            if type(metric) is bool or (type(metric) in (int, float) and math.isfinite(metric) and metric >= 0):
                safe[key] = metric
        if safe:
            result["coverage"] = safe
    return result


def sanitize_document_metadata(value: Any) -> Any:
    """Keep every page/tile receipt, but never retain recognized text or API bodies.

    Generic log sanitization truncates lists to 20; document provenance needs
    all pages. This restricted field contains only scalar metrics, identities,
    routing and artifact paths produced by the local document/OCR providers.
    """
    if isinstance(value, dict):
        return {str(k): sanitize_document_metadata(v) for k, v in value.items() if not is_sensitive_key(str(k)) and str(k) not in {"text", "content", "prompt", "raw_text", "snippet", "message", "messages"}}
    if isinstance(value, (list, tuple)):
        return [sanitize_document_metadata(v) for v in value]
    if isinstance(value, str):
        return truncate_text(sanitize_url_text(value))
    return value
