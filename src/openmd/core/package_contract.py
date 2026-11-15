"""Shared constants and helpers for Openmd V1 package artifacts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from .filename import safe_filename, short_hash
from .models import ContentItem, TranscriptResult
from .secrets import sanitize_url


OPENMD_PACKAGE_VERSION = "1.0-draft"
PACKAGE_SCHEMA_VERSION = "1.0-draft"
LEGACY_EXTRACTION_CONTRACT_VERSION = "1"
ACCEPTED_REQUIRED_COMPONENT_STATUSES: tuple[str, ...] = (
    "verified",
    "unavailable",
    "not_applicable",
)
XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION = "xhs-mm-v2"
XIAOHONGSHU_REQUIRED_COMPONENTS_BY_MEDIA_KIND: dict[str, tuple[str, ...]] = {
    "video": ("native_text", "video_transcript"),
    "image_text": ("native_text", "image_ocr"),
    "mixed": ("native_text",),
}

PACKAGE_PROFILES: tuple[str, ...] = (
    "article",
    "media",
    "conversation",
    "document",
    "process",
)
PROFILE_SECTIONS: dict[str, str] = {
    "article": "Content",
    "media": "Transcript",
    "conversation": "Conversation",
    "document": "Content",
    "process": "Steps",
}
PACKAGE_STATUS_VALUES: tuple[str, ...] = ("verified", "partial", "failed")
PACKAGE_SOURCE_LEVEL_VALUES: tuple[str, ...] = (
    "native_text",
    "native_caption",
    "auto_caption",
    "platform_transcript",
    "third_party_extractor",
    "browser_rendered",
    "manual_export",
    "ocr",
    "asr",
    "partial_extraction",
)
PERMISSION_SCOPE_VALUES: tuple[str, ...] = (
    "public",
    "user_accessible",
    "local_file",
    "user_export",
    "manual_export",
    "unknown",
)

REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "openmd_version",
    "package_schema_version",
    "package_id",
    "title",
    "profile",
    "modalities",
    "status",
    "source_level",
    "created_at",
    "updated_at",
    "language",
    "backend",
    "warnings",
)
REQUIRED_SOURCE_FIELDS: tuple[str, ...] = (
    "source_id",
    "source_type",
    "platform",
    "input_ref",
    "capture_method",
    "permission_scope",
    "captured_at",
    "adapter",
    "source_map",
)

_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9._-]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_date_parts(timestamp: str | None = None) -> tuple[str, str, str]:
    if timestamp:
        normalized = timestamp.replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            value = datetime.now(timezone.utc)
    else:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return f"{value.year:04d}", f"{value.month:02d}", value.date().isoformat()


def safe_identifier(value: str | None, *, fallback: str = "item") -> str:
    text = safe_filename(value, fallback=fallback, max_length=120)
    text = _IDENTIFIER_RE.sub("_", text).strip("._-")
    return text or fallback


def package_profile_for_item(item: ContentItem, result: TranscriptResult | None = None) -> str:
    profile = item.raw_meta.get("profile") if isinstance(item.raw_meta, dict) else None
    if isinstance(profile, str) and profile in PACKAGE_PROFILES:
        return profile
    if item.source_type in {"video", "audio"}:
        return "media"
    if item.source_type == "conversation":
        return "conversation"
    if item.source_type in {"document", "image_text"}:
        return "document"
    if item.source_type == "process":
        return "process"
    if result and result.metadata.get("profile") in PACKAGE_PROFILES:
        return str(result.metadata["profile"])
    return "article"


def package_source_level(source_level: str) -> str:
    if source_level in {"media_asr", "live_capture_asr"}:
        return "asr"
    if source_level in PACKAGE_SOURCE_LEVEL_VALUES:
        return source_level
    return "partial_extraction"


def modalities_for_item(item: ContentItem, profile: str) -> list[str]:
    if item.source_type == "video":
        return ["video", "audio", "text"]
    if item.source_type == "audio":
        return ["audio", "text"]
    if item.source_type == "image_text":
        return ["image", "text"]
    if item.source_type == "document":
        return ["document", "text"]
    if profile == "conversation":
        return ["text"]
    return ["text"]


def normalized_input_ref(item: ContentItem) -> str:
    value = str(item.url)
    if value.startswith("http://") or value.startswith("https://"):
        return sanitize_url(value)
    return value


def content_id_for_item(item: ContentItem) -> str:
    native_id = item.id
    if not native_id and isinstance(item.raw_meta, dict):
        native_id = item.raw_meta.get("content_id")
    if native_id:
        return safe_identifier(str(native_id), fallback=short_hash(normalized_input_ref(item)))
    return short_hash(f"{item.platform}:{normalized_input_ref(item)}", length=12)


def package_id_for_item(item: ContentItem) -> str:
    return f"{safe_identifier(item.platform, fallback='platform')}_{content_id_for_item(item)}"


def package_dir_name(
    item: ContentItem,
    result: TranscriptResult,
    *,
    created_at: str,
    version_suffix: str | None = None,
) -> str:
    title = result.title or item.title
    if not title and isinstance(item.raw_meta, dict):
        title = item.raw_meta.get("title")
    _, _, date_part = utc_date_parts(item.published_at or result.published_at or created_at)
    platform = safe_identifier(item.platform, fallback="platform")
    title_part = safe_filename(title, fallback=content_id_for_item(item), max_length=80)
    identity_hash = short_hash(f"{package_id_for_item(item)}:{normalized_input_ref(item)}")
    suffix = f"__{version_suffix}" if version_suffix else ""
    return f"{date_part}__{platform}__{title_part}__{identity_hash}{suffix}.openmd"


def package_root_for_args(out_dir: str | Path, package_root: str | Path | None) -> Path:
    if package_root is not None:
        return Path(package_root)
    return Path(out_dir) / "packages"


def dedupe_key_for_item(item: ContentItem) -> str:
    return f"{safe_identifier(item.platform, fallback='platform')}:{normalized_input_ref(item)}"


def compact_warning_list(values: list[Any]) -> list[str]:
    warnings: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in warnings:
            warnings.append(text)
    return warnings


def extraction_contract_version_for(
    item: ContentItem,
    result: TranscriptResult | None = None,
) -> str:
    if result is not None and result.extraction_contract_version:
        return str(result.extraction_contract_version)
    if isinstance(item.raw_meta, dict) and item.raw_meta.get("extraction_contract_version"):
        return str(item.raw_meta["extraction_contract_version"])
    return LEGACY_EXTRACTION_CONTRACT_VERSION


def required_component_ids_for_item(item: ContentItem) -> tuple[str, ...]:
    if not isinstance(item.raw_meta, dict):
        return ()
    raw = item.raw_meta.get("required_components")
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            component_id = entry.strip()
        elif isinstance(entry, dict) and entry.get("required", True):
            component_id = str(entry.get("id") or "").strip()
        else:
            component_id = ""
        if component_id and component_id not in values:
            values.append(component_id)
    return tuple(values)


def package_metadata_satisfies_extraction_contract(
    metadata: dict[str, Any],
    item: ContentItem,
) -> bool:
    requested_version = extraction_contract_version_for(item)
    stored_version = metadata.get("extraction_contract_version")
    if stored_version is None:
        return (
            requested_version == LEGACY_EXTRACTION_CONTRACT_VERSION
            and not required_component_ids_for_item(item)
        )
    if str(stored_version) != requested_version:
        return False

    required_ids = required_component_ids_for_item(item)
    if not required_ids:
        return True
    raw_components = metadata.get("components")
    if not isinstance(raw_components, list):
        return False
    components = {
        str(component.get("id")): component
        for component in raw_components
        if isinstance(component, dict) and component.get("id")
    }
    return all(
        component_id in components and _component_satisfies_requirement(components[component_id])
        for component_id in required_ids
    )


def apply_platform_extraction_contract(item: ContentItem) -> ContentItem:
    """Attach the platform contract before skip-existing runs."""
    if item.platform != "xiaohongshu":
        return item
    raw_meta = dict(item.raw_meta) if isinstance(item.raw_meta, dict) else {}
    has_contract_marker = bool(raw_meta.get("extraction_contract_version"))
    has_required_components = bool(required_component_ids_for_item(item))
    media_kind = str(raw_meta.get("media_kind") or "").strip()
    if not has_contract_marker and not has_required_components and not media_kind:
        return item
    if not has_required_components and media_kind:
        required = XIAOHONGSHU_REQUIRED_COMPONENTS_BY_MEDIA_KIND.get(media_kind)
        if required:
            raw_meta["required_components"] = list(required)
            has_required_components = True
    if has_contract_marker or has_required_components:
        raw_meta.setdefault(
            "extraction_contract_version", XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION
        )
    if raw_meta == item.raw_meta:
        return item
    return replace(item, raw_meta=raw_meta)


def extraction_result_satisfies_contract(
    item: ContentItem,
    result: TranscriptResult,
) -> bool:
    metadata = {
        "extraction_contract_version": extraction_contract_version_for(item, result),
        "components": [
            {
                "id": component.id,
                "status": component.status,
                "terminal_reason": component.terminal_reason,
                "error_code": component.error_code,
                "failure_class": component.failure_class,
            }
            for component in result.normalized_components
        ],
    }
    return package_metadata_satisfies_extraction_contract(metadata, item)


def _component_satisfies_requirement(component: dict[str, Any]) -> bool:
    status = component.get("status")
    if status in {"verified", "not_applicable"}:
        return True
    if status != "unavailable":
        return False
    return (
        isinstance(component.get("terminal_reason"), str)
        and bool(str(component["terminal_reason"]).strip())
        and isinstance(component.get("error_code"), str)
        and bool(str(component["error_code"]).strip())
        and component.get("failure_class") == "source_unavailable"
    )
