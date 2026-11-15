"""Append-only lightweight runlog writer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ContentItem, FAILURE_CLASS_VALUES, TranscriptResult
from .package_contract import extraction_contract_version_for
from .secrets import asr_execution_metrics, sanitize_runlog_entry, sanitize_source_scope


USER_RESOLVABLE_ERROR_TERMS: tuple[str, ...] = (
    "auth",
    "credential",
    "login",
    "permission",
    "selector",
    "target_scope",
    "api_key_missing",
    "session_expired",
    "cache_stale",
    "refresh_needed",
    "export_required",
    "target_missing",
)
SOURCE_UNAVAILABLE_ERROR_TERMS: tuple[str, ...] = (
    "source_unavailable",
    "source_empty",
    "not_found",
    "removed",
    "deleted",
    "empty_result",
    "empty_content",
    "no_transcript",
    "subtitle_missing",
    "no_delta",
    "no_data",
)
RUNTIME_TRANSIENT_ERROR_TERMS: tuple[str, ...] = (
    "timeout",
    "rate_limit",
    "locked",
    "unreachable",
    "temporarily",
    "http_5",
    "backend_exit",
    "fetch_failed",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_runlog_path(out_dir: str | Path) -> Path:
    return Path(out_dir) / "runlog.jsonl"


def append_runlog_entry(path: str | Path, entry: dict[str, Any]) -> dict[str, Any]:
    runlog_path = Path(path)
    runlog_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_runlog_entry(entry)
    with runlog_path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(sanitized, ensure_ascii=False, sort_keys=True) + "\n")
    return sanitized


def build_runlog_entry(
    *,
    input_url: str,
    platform: str,
    backend: str,
    status: str,
    source_type: str,
    source_level: str,
    output_path: str | Path | None = None,
    package_id: str | None = None,
    package_operation: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    item_count: int = 1,
    success_count: int | None = None,
    failed_count: int | None = None,
    event_type: str = "item",
    failure_class: str | None = None,
    guardrail_signals: list[str] | None = None,
    resume_pointer: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    extraction_contract_version: str | None = None,
    components: list[dict[str, Any]] | None = None,
    terminal_gap_count: int = 0,
    route_trace: dict[str, Any] | None = None,
    document_metadata: dict[str, Any] | None = None,
    asr_execution: dict[str, Any] | None = None,
    source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    success = success_count if success_count is not None else int(status in {"verified", "partial"})
    failed = failed_count if failed_count is not None else int(status == "failed")
    normalized_failure_class = normalize_failure_class(failure_class) or classify_failure(error_code)
    return {
        "event_type": event_type,
        "input_url": input_url,
        "platform": platform,
        "backend": backend,
        "status": status,
        "source_type": source_type,
        "source_level": source_level,
        "failure_class": normalized_failure_class,
        "guardrail_signals": guardrail_signals or [],
        "resume_pointer": resume_pointer,
        "output_path": str(output_path) if output_path else "",
        "package_id": package_id,
        "package_operation": package_operation,
        "started_at": started_at or utc_now_iso(),
        "finished_at": finished_at or utc_now_iso(),
        "item_count": item_count,
        "success_count": success,
        "failed_count": failed,
        "error_code": error_code,
        "error_summary": error_summary,
        "extraction_contract_version": extraction_contract_version,
        "components": components or [],
        "terminal_gap_count": terminal_gap_count,
        "route_trace": route_trace or {},
        "document_metadata": document_metadata or {},
        "asr_execution": asr_execution or {},
        "source_scope": sanitize_source_scope(source_scope),
    }


def build_item_runlog_entry(
    item: ContentItem,
    result: TranscriptResult,
    *,
    output_path: str | Path | None = None,
    package_id: str | None = None,
    package_operation: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    failure_class = None
    if isinstance(result.metadata, dict):
        raw_failure_class = result.metadata.get("failure_class")
        if isinstance(raw_failure_class, str):
            failure_class = raw_failure_class
    return build_runlog_entry(
        input_url=item.url,
        platform=item.platform,
        backend=result.backend,
        status=result.status,
        source_type=item.source_type,
        source_level=result.source_level,
        asr_execution=asr_execution_metrics(result.metadata),
        source_scope=result.metadata,
        route_trace={key: result.metadata[key] for key in ("card_id", "route_version", "overlay_applied", "fallback_attempts") if key in result.metadata},
        document_metadata={key: result.metadata[key] for key in ("pages", "office_loss_check", "office_recovery", "office_external_resources_fetched", "source_format", "mixed_routes", "pymupdf_version", "terminal_partial") if key in result.metadata} if item.platform in {"screenshot", "pdf"} else {},
        output_path=output_path,
        package_id=package_id,
        package_operation=package_operation,
        started_at=started_at,
        finished_at=finished_at,
        failure_class=failure_class,
        error_code=result.error_code,
        error_summary=result.error_summary,
        extraction_contract_version=extraction_contract_version_for(item, result),
        components=[
            {
                "id": component.id,
                "kind": component.kind,
                "status": component.status,
                "source_level": component.source_level,
                "backend": component.backend,
                "text_ref": component.text_ref,
                "warnings": component.warnings,
                "terminal_reason": component.terminal_reason,
                "error_code": component.error_code,
                "failure_class": component.failure_class,
            }
            for component in result.normalized_components
        ],
        terminal_gap_count=sum(
            component.status == "unavailable" for component in result.normalized_components
        ),
    )


def normalize_failure_class(value: str | None) -> str | None:
    if value in FAILURE_CLASS_VALUES:
        return value
    return None


def classify_failure(error_code: str | None) -> str | None:
    """Map backend-specific error codes into the shared V1 recovery taxonomy."""
    if not error_code:
        return None
    normalized = str(error_code).lower()
    if any(term in normalized for term in USER_RESOLVABLE_ERROR_TERMS):
        return "user_resolvable"
    if any(term in normalized for term in RUNTIME_TRANSIENT_ERROR_TERMS):
        return "runtime_transient"
    if any(term in normalized for term in SOURCE_UNAVAILABLE_ERROR_TERMS):
        return "source_unavailable"
    return "code_resolvable"
