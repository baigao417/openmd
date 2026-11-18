"""Small reusable counters for collection coverage summaries.

The helper intentionally keeps only bounded enums and integer counts.  It does
not retain URLs, output paths, backend payloads, or other acquisition data, so
platform summaries can safely serialize the result without leaking transient
locators.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import ContentItem, ExtractionComponent, TranscriptResult
from .package_contract import package_source_level


_ITEM_STATUSES = ("verified", "partial", "failed")
_BLOCKING_COMPONENT_STATUSES = {"partial", "failed", "retryable", "auth_challenge"}
_CONTENT_TYPES = ("text", "image_text", "video", "mixed")


@dataclass
class CollectionCoverage:
    """Accumulate item outcomes and component provenance without identifiers."""

    attempted: int = 0
    skipped_existing: int = 0
    terminal_gap_items: int = 0
    retryable_items: int = 0
    _item_statuses: Counter[str] = field(default_factory=Counter)
    _component_statuses: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    _component_sources: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    _source_levels: Counter[str] = field(default_factory=Counter)
    _completeness: Counter[str] = field(default_factory=Counter)

    def record_result(
        self,
        result: TranscriptResult,
        *,
        skipped_existing: bool = False,
        force_failed: bool = False,
    ) -> None:
        self._record_completeness(result.metadata)
        self.record(
            status="failed" if force_failed else result.status,
            components=result.normalized_components,
            skipped_existing=skipped_existing,
        )

    def record_metadata(
        self,
        metadata: Mapping[str, Any],
        *,
        skipped_existing: bool = False,
    ) -> None:
        self._record_completeness(metadata)
        raw_components = metadata.get("components")
        components = raw_components if isinstance(raw_components, list) else []
        self.record(
            status=str(metadata.get("status") or "failed"),
            components=components,
            skipped_existing=skipped_existing,
        )

    def record_failure(self) -> None:
        self.record(status="failed", components=[])

    def _record_completeness(self, metadata: Mapping[str, Any]) -> None:
        completeness = metadata.get("completeness")
        if isinstance(completeness, dict) and completeness.get("status") in {"ok", "downgrade", "unknown"}:
            self._completeness[completeness["status"]] += 1

    def record(
        self,
        *,
        status: str,
        components: Iterable[ExtractionComponent | Mapping[str, Any]],
        skipped_existing: bool = False,
    ) -> None:
        normalized_status = status if status in _ITEM_STATUSES else "failed"
        self.attempted += 1
        self._item_statuses[normalized_status] += 1
        if skipped_existing:
            self.skipped_existing += 1

        has_terminal_gap = False
        has_blocking_component = normalized_status == "failed"
        for component in components:
            component_id, component_status, source_level = _component_projection(component)
            if not component_id:
                continue
            source_level = package_source_level(source_level)
            self._component_statuses[component_id][component_status] += 1
            self._component_sources[component_id][source_level] += 1
            self._source_levels[source_level] += 1
            has_terminal_gap = has_terminal_gap or component_status == "unavailable"
            has_blocking_component = (
                has_blocking_component
                or component_status in _BLOCKING_COMPONENT_STATUSES
            )

        if has_terminal_gap:
            self.terminal_gap_items += 1
        if has_blocking_component:
            self.retryable_items += 1

    def as_dict(self) -> dict[str, Any]:
        component_counts: dict[str, dict[str, int]] = {}
        component_source_counts: dict[str, dict[str, int]] = {}
        for component_id in sorted(self._component_statuses):
            statuses = self._component_statuses[component_id]
            component_counts[component_id] = {
                "total": sum(statuses.values()),
                **{key: int(value) for key, value in sorted(statuses.items())},
            }
            component_source_counts[component_id] = {
                key: int(value)
                for key, value in sorted(self._component_sources[component_id].items())
            }

        return {
            "attempted": self.attempted,
            "completeness_counts": dict(sorted(self._completeness.items())),
            "verified": int(self._item_statuses["verified"]),
            "partial": int(self._item_statuses["partial"]),
            "failed": int(self._item_statuses["failed"]),
            "skipped_existing": self.skipped_existing,
            "terminal_gaps": sum(
                counts["unavailable"] for counts in self._component_statuses.values()
            ),
            "terminal_gap_items": self.terminal_gap_items,
            "retryable": sum(
                counts["retryable"] for counts in self._component_statuses.values()
            ),
            "retryable_items": self.retryable_items,
            "component_counts": component_counts,
            "component_source_counts": component_source_counts,
            "source_counts": {
                key: int(value) for key, value in sorted(self._source_levels.items())
            },
        }


def collection_content_type_counts(items: Iterable[ContentItem]) -> dict[str, int]:
    """Return stable text/image_text/video/mixed counts for discovered items."""
    counts = Counter({key: 0 for key in _CONTENT_TYPES})
    for item in items:
        raw_kind = item.raw_meta.get("media_kind") if isinstance(item.raw_meta, dict) else None
        kind = str(raw_kind or item.source_type or "mixed").strip().lower().replace("-", "_")
        if kind in {"article", "document", "text_first", "text_only"}:
            kind = "text"
        if kind not in _CONTENT_TYPES:
            kind = "mixed"
        counts[kind] += 1
    return {key: int(counts[key]) for key in _CONTENT_TYPES}


def _component_projection(
    component: ExtractionComponent | Mapping[str, Any],
) -> tuple[str, str, str]:
    if isinstance(component, ExtractionComponent):
        return component.id, component.status, component.source_level
    component_id = str(component.get("id") or "").strip()
    status = str(component.get("status") or "failed").strip()
    source_level = str(component.get("source_level") or "partial_extraction").strip()
    return component_id, status, source_level
