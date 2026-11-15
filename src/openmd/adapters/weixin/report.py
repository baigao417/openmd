"""Weixin author-batch coverage report helpers.

The report consumes already-sanitized Openmd runlog JSONL rows.  It does not
call the WeChat sidecar, read browser/session state, or persist raw exporter
responses.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openmd.core.secrets import sanitize_error_summary, sanitize_url_text


WEIXIN_FALLBACK_BACKENDS: frozenset[str] = frozenset(
    {
        "wechat-article-exporter.download",
        "opencli.weixin.download",
        "opencli.web.read",
    }
)


@dataclass(frozen=True)
class WeixinAuthorBatchReportResult:
    report: dict[str, Any]
    json_path: Path | None = None
    markdown_path: Path | None = None


def load_runlog_entries(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path_value in paths:
        path = Path(path_value)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    entries.append(payload)
    return entries


def build_weixin_author_batch_report(
    entries: Iterable[dict[str, Any]],
    *,
    title: str = "Weixin author batch coverage report",
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ignored_rows = 0
    for row in entries:
        if row.get("platform") != "weixin" or row.get("event_type", "item") != "item":
            ignored_rows += 1
            continue
        input_url = _safe_text(row.get("input_url"))
        if not input_url:
            ignored_rows += 1
            continue
        grouped[input_url].append(_sanitize_report_row(row))

    item_reports = [_item_report(input_url, attempts) for input_url, attempts in grouped.items()]
    item_reports.sort(key=lambda item: item["input_url"])

    final_status_counts = Counter(item["final_status"] for item in item_reports)
    final_failure_class_counts = Counter(
        item["final_failure_class"] for item in item_reports if item.get("final_failure_class")
    )
    final_error_code_counts = Counter(
        item["final_error_code"] for item in item_reports if item.get("final_error_code")
    )
    final_backend_counts = Counter(item["final_backend"] for item in item_reports if item.get("final_backend"))
    all_error_code_counts: Counter[str] = Counter()
    fallback_backend_counts: Counter[str] = Counter()
    for item in item_reports:
        for attempt_index, attempt in enumerate(item["attempts"]):
            error_code = attempt.get("error_code")
            backend = attempt.get("backend")
            if error_code:
                all_error_code_counts[str(error_code)] += 1
            if attempt_index > 0 and backend in WEIXIN_FALLBACK_BACKENDS:
                fallback_backend_counts[str(backend)] += 1

    covered_items = [item for item in item_reports if item["covered"]]
    recovered_items = [item for item in item_reports if item["recovered"]]
    missing_items = [item for item in item_reports if item["missing"]]
    fallback_attempt_items = [item for item in item_reports if item["fallback_attempt_count"] > 0]

    return {
        "title": title,
        "summary": {
            "total_items": len(item_reports),
            "covered_count": len(covered_items),
            "verified_count": final_status_counts.get("verified", 0),
            "partial_count": final_status_counts.get("partial", 0),
            "failed_count": final_status_counts.get("failed", 0),
            "recovered_count": len(recovered_items),
            "missing_count": len(missing_items),
            "fallback_attempt_item_count": len(fallback_attempt_items),
            "fallback_attempt_count": sum(item["fallback_attempt_count"] for item in item_reports),
            "ignored_rows": ignored_rows,
        },
        "final_status_counts": dict(sorted(final_status_counts.items())),
        "final_failure_class_counts": dict(sorted(final_failure_class_counts.items())),
        "final_error_code_counts": dict(sorted(final_error_code_counts.items())),
        "final_backend_counts": dict(sorted(final_backend_counts.items())),
        "all_attempt_error_code_counts": dict(sorted(all_error_code_counts.items())),
        "fallback_backend_counts": dict(sorted(fallback_backend_counts.items())),
        "recovered_items": [_compact_item(item) for item in recovered_items],
        "missing_items": [_compact_item(item) for item in missing_items],
        "other_failed_items": [
            _compact_item(item)
            for item in item_reports
            if item["final_status"] == "failed" and not item["missing"]
        ],
        "items": item_reports,
    }


def write_weixin_author_batch_report(
    entries: Iterable[dict[str, Any]],
    out_dir: str | Path,
    *,
    title: str = "Weixin author batch coverage report",
    write_json: bool = True,
    write_markdown: bool = True,
) -> WeixinAuthorBatchReportResult:
    report = build_weixin_author_batch_report(entries, title=title)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path: Path | None = None
    markdown_path: Path | None = None
    if write_json:
        json_path = output_dir / "weixin-author-batch-report.json"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if write_markdown:
        markdown_path = output_dir / "weixin-author-batch-report.md"
        markdown_path.write_text(format_weixin_author_batch_report_markdown(report), encoding="utf-8")
    return WeixinAuthorBatchReportResult(report=report, json_path=json_path, markdown_path=markdown_path)


def format_weixin_author_batch_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"# {report.get('title') or 'Weixin author batch coverage report'}",
        "",
        "## Summary",
        "",
        f"- Total items: {summary.get('total_items', 0)}",
        f"- Covered: {summary.get('covered_count', 0)}",
        f"- Verified: {summary.get('verified_count', 0)}",
        f"- Partial: {summary.get('partial_count', 0)}",
        f"- Failed: {summary.get('failed_count', 0)}",
        f"- Recovered: {summary.get('recovered_count', 0)}",
        f"- Missing/source unavailable: {summary.get('missing_count', 0)}",
        f"- Fallback attempt items: {summary.get('fallback_attempt_item_count', 0)}",
        f"- Fallback attempts: {summary.get('fallback_attempt_count', 0)}",
        "",
    ]
    lines.extend(_counter_section("Final failure classes", report.get("final_failure_class_counts", {})))
    lines.extend(_counter_section("Final error codes", report.get("final_error_code_counts", {})))
    lines.extend(_counter_section("Fallback backends", report.get("fallback_backend_counts", {})))
    lines.extend(_items_section("Recovered items", report.get("recovered_items", [])))
    lines.extend(_items_section("Missing items", report.get("missing_items", [])))
    lines.extend(_items_section("Other failed items", report.get("other_failed_items", [])))
    lines.append("")
    return "\n".join(lines)


def _item_report(input_url: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    final = attempts[-1]
    final_status = _safe_text(final.get("status")) or "failed"
    covered = final_status in {"verified", "partial"}
    final_error_code = _safe_text(final.get("error_code"))
    final_failure_class = _safe_text(final.get("failure_class"))
    had_failed_attempt = any(_safe_text(attempt.get("status")) == "failed" for attempt in attempts[:-1])
    recovered = covered and had_failed_attempt
    missing = (
        final_status == "failed"
        and (
            final_failure_class == "source_unavailable"
            or _is_source_empty_error(final_error_code)
        )
    )
    fallback_attempt_count = max(0, len(attempts) - 1)
    return {
        "input_url": sanitize_url_text(input_url),
        "covered": covered,
        "recovered": recovered,
        "missing": missing,
        "attempt_count": len(attempts),
        "fallback_attempt_count": fallback_attempt_count,
        "final_backend": _safe_text(final.get("backend")),
        "final_status": final_status,
        "final_source_level": _safe_text(final.get("source_level")),
        "final_failure_class": final_failure_class,
        "final_error_code": final_error_code,
        "final_error_summary": sanitize_error_summary(final.get("error_summary")),
        "output_path": _safe_text(final.get("output_path")),
        "package_id": _safe_text(final.get("package_id")),
        "package_operation": _safe_text(final.get("package_operation")),
        "attempts": attempts,
    }


def _sanitize_report_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": _safe_text(row.get("backend")),
        "status": _safe_text(row.get("status")),
        "source_level": _safe_text(row.get("source_level")),
        "failure_class": _safe_text(row.get("failure_class")),
        "error_code": _safe_text(row.get("error_code")),
        "error_summary": sanitize_error_summary(row.get("error_summary")),
        "output_path": _safe_text(row.get("output_path")),
        "package_id": _safe_text(row.get("package_id")),
        "package_operation": _safe_text(row.get("package_operation")),
    }


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_url": item["input_url"],
        "final_backend": item["final_backend"],
        "final_status": item["final_status"],
        "final_failure_class": item["final_failure_class"],
        "final_error_code": item["final_error_code"],
        "attempt_count": item["attempt_count"],
        "fallback_attempt_count": item["fallback_attempt_count"],
        "package_id": item.get("package_id") or None,
    }


def _counter_section(title: str, values: dict[str, Any]) -> list[str]:
    lines = [f"## {title}", ""]
    if not values:
        lines.extend(["- None", ""])
        return lines
    for key, value in values.items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")
    return lines


def _items_section(title: str, items: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.extend(["- None", ""])
        return lines
    for item in items[:50]:
        lines.append(
            "- "
            f"{item.get('input_url')} | "
            f"backend={item.get('final_backend')} | "
            f"status={item.get('final_status')} | "
            f"failure_class={item.get('final_failure_class')} | "
            f"error={item.get('final_error_code')} | "
            f"attempts={item.get('attempt_count')}"
        )
    if len(items) > 50:
        lines.append(f"- ... {len(items) - 50} more omitted from Markdown view")
    lines.append("")
    return lines


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return sanitize_url_text(str(value).strip())


def _is_source_empty_error(value: str) -> bool:
    normalized = value.lower()
    return "source_empty" in normalized or "empty_result" in normalized or "not_found" in normalized
