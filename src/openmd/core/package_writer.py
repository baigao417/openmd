"""Writer for V1 ``item.openmd/`` artifact packages."""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .filename import short_hash
from .execution_policy import require_matching_fingerprint
from .models import ContentItem, TranscriptResult, block_as_dict
from .readable import format_transcript
from .package_contract import (
    OPENMD_PACKAGE_VERSION,
    PACKAGE_SCHEMA_VERSION,
    PROFILE_SECTIONS,
    compact_warning_list,
    content_id_for_item,
    dedupe_key_for_item,
    extraction_contract_version_for,
    extraction_result_satisfies_contract,
    modalities_for_item,
    normalized_input_ref,
    package_dir_name,
    package_id_for_item,
    package_metadata_satisfies_extraction_contract,
    package_profile_for_item,
    package_root_for_args,
    package_source_level,
    utc_date_parts,
    utc_now_iso,
)
from .package_validator import PackageValidationError, validate_package_or_raise
from .secrets import sanitize_error_summary, sanitize_url_text


@dataclass(frozen=True)
class PackageWriteResult:
    package_path: Path
    package_id: str
    operation: str
    skipped_existing: bool = False


def write_item_package(
    item: ContentItem,
    result: TranscriptResult,
    out_dir: str | Path,
    *,
    package_root: str | Path | None = None,
    skip_existing: bool = True,
    update: bool = False,
    overwrite: bool = False,
    new_version: bool = False,
    captured_at: str | None = None,
    execution_fingerprint: str | None = None,
) -> PackageWriteResult:
    """Write an extracted item as an ``.openmd`` package.

    Default reruns are idempotent: if the same ``package_id`` already exists,
    the package is returned without changing files. ``update`` and
    ``overwrite`` both update the existing directory in place. ``new_version``
    creates a sibling snapshot with the same stable ``package_id``.
    """

    root = package_root_for_args(out_dir, package_root)
    package_id = package_id_for_item(item)
    existing = find_existing_package_for_item(item, out_dir, package_root=package_root)
    existing_path = existing.package_path if existing else None
    existing_satisfies_contract = bool(
        existing_path and _existing_package_satisfies_contract(existing_path, item)
    )
    if existing_path and execution_fingerprint:
        validate_package_or_raise(existing_path)
        metadata = json.loads((existing_path / "metadata.json").read_text(encoding="utf-8"))
        mismatch = require_matching_fingerprint(metadata, execution_fingerprint)
        if mismatch or not existing_satisfies_contract:
            raise PackageValidationError(mismatch or "extraction_contract_mismatch")

    if (
        existing_path
        and skip_existing
        and not update
        and not overwrite
        and not new_version
        and existing_satisfies_contract
    ):
        validate_package_or_raise(existing_path)
        return existing

    if not extraction_result_satisfies_contract(item, result):
        raise PackageValidationError(
            "extraction result does not satisfy the requested extraction contract"
        )

    now = captured_at or utc_now_iso()
    contract_upgrade = bool(
        existing_path
        and skip_existing
        and not update
        and not overwrite
        and not new_version
        and not existing_satisfies_contract
    )
    created_at = (
        _existing_created_at(existing_path)
        if existing_path and (update or overwrite or contract_upgrade)
        else now
    )

    if existing_path and (update or overwrite or contract_upgrade) and not new_version:
        package_path = existing_path
        operation = (
            "upgraded_contract"
            if contract_upgrade
            else ("updated" if update else "overwritten")
        )
    else:
        version_suffix = _version_suffix(now) if existing_path and new_version else None
        package_path = _new_package_path(root, item, result, created_at=created_at, version_suffix=version_suffix)
        operation = "new_version" if existing_path and new_version else "created"

    staging_path = _staging_package_path(package_path)
    _write_package_files(
        staging_path,
        item,
        result,
        created_at=created_at,
        updated_at=now,
        execution_fingerprint=execution_fingerprint,
    )
    try:
        validate_package_or_raise(staging_path)
    except Exception:
        shutil.rmtree(staging_path, ignore_errors=True)
        raise

    _publish_validated_package(
        staging_path,
        package_path,
        replace_existing=package_path.exists(),
    )
    return PackageWriteResult(
        package_path=package_path,
        package_id=package_id,
        operation=operation,
        skipped_existing=False,
    )


def find_existing_package_for_item(
    item: ContentItem,
    out_dir: str | Path,
    *,
    package_root: str | Path | None = None,
) -> PackageWriteResult | None:
    root = package_root_for_args(out_dir, package_root)
    package_id = package_id_for_item(item)
    existing_path = _find_existing_package(root, package_id)
    if existing_path is None:
        return None
    return PackageWriteResult(
        package_path=existing_path,
        package_id=package_id,
        operation="skipped_existing",
        skipped_existing=True,
    )


def find_corrupt_package_for_item(
    item: ContentItem,
    out_dir: str | Path,
    *,
    package_root: str | Path | None = None,
) -> Path | None:
    """Find an unreadable package whose immutable path identity matches ``item``.

    A compact run must fail closed instead of treating a broken metadata file as
    a missing package and publishing a second package for the same input.
    """
    root = package_root_for_args(out_dir, package_root)
    if not root.exists():
        return None
    identity_hash = short_hash(f"{package_id_for_item(item)}:{normalized_input_ref(item)}")
    for package_path in sorted(root.rglob("*.openmd")):
        if ".tmp-" in package_path.name or ".bak-" in package_path.name:
            continue
        name = package_path.name
        if not (name.endswith(f"__{identity_hash}.openmd") or f"__{identity_hash}__v" in name):
            continue
        try:
            metadata = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return package_path
        if not isinstance(metadata, dict) or metadata.get("package_id") != package_id_for_item(item):
            return package_path
    return None


def _new_package_path(
    root: Path,
    item: ContentItem,
    result: TranscriptResult,
    *,
    created_at: str,
    version_suffix: str | None = None,
) -> Path:
    year, month, _ = utc_date_parts(item.published_at or result.published_at or created_at)
    directory = root / year / month
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / package_dir_name(item, result, created_at=created_at, version_suffix=version_suffix)
    if not candidate.exists():
        return candidate

    counter = 2
    base_name = candidate.name.removesuffix(".openmd")
    while True:
        next_candidate = directory / f"{base_name}-{counter}.openmd"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def _write_package_files(
    package_path: Path,
    item: ContentItem,
    result: TranscriptResult,
    *,
    created_at: str,
    updated_at: str,
    execution_fingerprint: str | None,
) -> None:
    package_path.mkdir(parents=True, exist_ok=True)
    (package_path / "assets").mkdir(exist_ok=True)
    metadata = build_package_metadata(
        item,
        result,
        created_at=created_at,
        updated_at=updated_at,
        execution_fingerprint=execution_fingerprint,
    )
    sources = build_package_sources(item, result, captured_at=updated_at)
    index_md = render_package_index(item, result, metadata=metadata, sources=sources)

    _write_json(package_path / "metadata.json", metadata)
    _write_json(package_path / "sources.json", sources)
    (package_path / "index.md").write_text(index_md, encoding="utf-8", newline="\n")

    if metadata["profile"] == "media":
        (package_path / "transcript.md").write_text(
            _render_transcript_sidecar(item, result),
            encoding="utf-8",
            newline="\n",
        )
    if metadata["profile"] == "conversation" and result.blocks:
        messages = []
        for index, block in enumerate(result.blocks, start=1):
            value = block_as_dict(block)
            if value.get("type") != "turn":
                continue
            messages.append({
                "id": value.get("message_id") or f"turn_{index:04d}",
                "timestamp": value.get("time") or "unknown",
                "speaker": value.get("role") or "unknown",
                "text": value.get("text") or "",
                "attachments": [],
                "links": [],
                "source_map": [{"target": f"index.md#turn-{index}", "source_ref": {"source_id": "src_001"}}],
            })
        if messages:
            _write_json(package_path / "messages.json", {"messages": messages})


def build_package_metadata(
    item: ContentItem,
    result: TranscriptResult,
    *,
    created_at: str,
    updated_at: str,
    execution_fingerprint: str | None = None,
) -> dict[str, Any]:
    profile = package_profile_for_item(item, result)
    title = result.title or item.title
    if not title and isinstance(item.raw_meta, dict):
        title = item.raw_meta.get("title")
    author = result.author or item.author
    published_at = result.published_at or item.published_at
    source_level = package_source_level(str(result.source_level))
    warnings = compact_warning_list([*result.source_notes])
    if result.error_summary:
        warnings.append(str(result.error_summary))

    metadata: dict[str, Any] = {
        "openmd_version": OPENMD_PACKAGE_VERSION,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_id_for_item(item),
        "content_id": content_id_for_item(item),
        "dedupe_key": dedupe_key_for_item(item),
        "title": _single_line_text(title or content_id_for_item(item)),
        "profile": profile,
        "modalities": modalities_for_item(item, profile),
        "status": result.status,
        "source_level": source_level,
        "extraction_contract_version": extraction_contract_version_for(item, result),
        "components": [
            {
                "id": component.id,
                "kind": component.kind,
                "status": component.status,
                "source_level": package_source_level(str(component.source_level)),
                "backend": component.backend,
                "text_ref": component.text_ref,
                "warnings": [
                    sanitize_url_text(value)
                    for value in compact_warning_list(component.warnings)
                ],
                "terminal_reason": component.terminal_reason,
                "error_code": component.error_code,
                "failure_class": component.failure_class,
            }
            for component in result.normalized_components
        ],
        "created_at": created_at,
        "updated_at": updated_at,
        "language": _metadata_language(item, result),
        "backend": {
            "name": result.backend,
            "route": "openmd",
            "version": None,
        },
        "warnings": [sanitize_url_text(value) for value in warnings],
        "source_platform": item.platform,
        "source_url": normalized_input_ref(item),
    }
    from .renderer import document_source_format
    source_format = document_source_format(item, result)
    if source_format:
        metadata["format"] = str(source_format).lower()
    if author:
        metadata["author"] = _single_line_text(author)
    if published_at:
        metadata["published_at"] = published_at
    if execution_fingerprint:
        metadata["execution_fingerprint"] = execution_fingerprint
    metadata["route_trace"] = {key: result.metadata[key] for key in ("card_id", "route_version", "overlay_applied") if key in result.metadata}
    from .secrets import asr_execution_metrics

    asr_execution = asr_execution_metrics(result.metadata)
    if asr_execution:
        metadata["asr_execution"] = asr_execution
    if item.platform in {"pdf", "screenshot"}:
        from .secrets import sanitize_document_metadata

        metadata["document_metadata"] = sanitize_document_metadata({key: result.metadata[key] for key in ("pages", "office_loss_check", "office_recovery", "office_external_resources_fetched", "source_format", "mixed_routes", "pymupdf_version", "terminal_partial", "hidden_text_removed_chars") if key in result.metadata})
    if result.blocks:
        metadata["blocks"] = [block_as_dict(block) for block in result.blocks]
    if item.platform == "ai_chat" and result.metadata.get("anchor_response_id"):
        try:
            metadata["anchor_response_id"] = str(UUID(str(result.metadata["anchor_response_id"])))
        except ValueError:
            pass  # Reply anchors are UUIDs; arbitrary query data is not persisted.
    return metadata


def build_package_sources(
    item: ContentItem,
    result: TranscriptResult,
    *,
    captured_at: str,
) -> dict[str, Any]:
    adapter_name = result.backend or f"openmd.{item.platform}"
    input_ref = normalized_input_ref(item)
    source = {
        "source_id": "src_001",
        "source_type": item.source_type,
        "platform": item.platform,
        "input_ref": input_ref,
        "capture_method": adapter_name,
        "permission_scope": _permission_scope(item),
        "captured_at": captured_at,
        "adapter": {
            "name": adapter_name,
            "version": None,
        },
        "source_map": _ocr_source_map_entries(item, result, input_ref) or [_primary_source_map_entry(item, result, input_ref)],
    }
    return {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "primary_source_id": "src_001",
        "sources": [source],
    }


def render_package_index(
    item: ContentItem,
    result: TranscriptResult,
    *,
    metadata: dict[str, Any],
    sources: dict[str, Any],
) -> str:
    profile = str(metadata["profile"])
    section = PROFILE_SECTIONS[profile]
    title = _single_line_text(str(metadata["title"]))
    source = sources["sources"][0]
    text = _package_text(item, result)
    if profile == "conversation" and result.blocks:
        from .renderer import render_conversation_blocks

        text = render_conversation_blocks(result.blocks, source_url=item.url, platform=item.platform, exported_at=metadata["updated_at"], coverage=str(result.metadata.get("coverage") or "visible_branch"), title=title).strip()
    if not text and result.status == "failed":
        text = sanitize_url_text(result.error_summary or "Extraction failed.")

    lines = [
        "---",
        f"title: {_yaml_scalar(title)}",
        f"profile: {profile}",
        f"source_platform: {_yaml_scalar(str(metadata['source_platform']))}",
        *([f"format: {metadata['format']}"] if metadata.get("format") else []),
        f"status: {metadata['status']}",
        f"source_level: {metadata['source_level']}",
        f"created_at: {metadata['created_at']}",
        "---",
        "",
        f"# {title}",
        "",
        "## Source",
        "",
        f"- Platform: {item.platform}",
        *([f"- Format: {metadata['format']}"] if metadata.get("format") else []),
        f"- Input: {source['input_ref']}",
        f"- Backend: {metadata['backend']['name']}",
        f"- Status: {metadata['status']}",
        f"- Source level: {metadata['source_level']}",
    ]
    if metadata.get("author"):
        lines.append(f"- Author: {metadata['author']}")
    if metadata.get("published_at"):
        lines.append(f"- Published at: {metadata['published_at']}")
    warnings = metadata.get("warnings") or []
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(["", f"## {section}", ""])
    lines.append(text or "_No extracted content._")
    lines.append("")
    return "\n".join(lines)


def _package_text(item: ContentItem, result: TranscriptResult) -> str:
    text = sanitize_url_text(result.text.strip())
    if item.platform == "xiaoe" and item.source_type in {"audio", "video"}:
        title = result.title or item.title
        if title and text.startswith(f"# {title}\n"):
            text = text.partition("\n")[2].lstrip("\n")
        if text.startswith("## Transcript\n"):
            text = text.partition("\n")[2].lstrip("\n")
        text = format_transcript(text, source_type=item.source_type, platform=item.platform)
    return text


def _render_transcript_sidecar(item: ContentItem, result: TranscriptResult) -> str:
    title = result.title or "Transcript"
    text = _package_text(item, result)
    section = "## Transcript\n\n" if item.platform == "xiaoe" else ""
    return f"# {title}\n\n{section}{text}\n"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _find_existing_package(root: Path, package_id: str) -> Path | None:
    if not root.exists():
        return None
    for metadata_path in sorted(root.rglob("metadata.json")):
        package_path = metadata_path.parent
        if package_path.suffix != ".openmd":
            continue
        if ".tmp-" in package_path.name or ".bak-" in package_path.name:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict) and metadata.get("package_id") == package_id:
            return package_path
    return None


def _staging_package_path(package_path: Path) -> Path:
    while True:
        candidate = package_path.with_name(f"{package_path.stem}.tmp-{uuid4().hex[:8]}.openmd")
        if not candidate.exists():
            return candidate


def _publish_validated_package(
    staging_path: Path,
    package_path: Path,
    *,
    replace_existing: bool,
) -> None:
    if not replace_existing:
        staging_path.rename(package_path)
        return

    backup_path = package_path.with_name(f"{package_path.stem}.bak-{uuid4().hex[:8]}.openmd")
    package_path.rename(backup_path)
    try:
        staging_path.rename(package_path)
    except Exception:
        if package_path.exists():
            shutil.rmtree(package_path, ignore_errors=True)
        if backup_path.exists():
            backup_path.rename(package_path)
        raise
    else:
        shutil.rmtree(backup_path, ignore_errors=True)


def _existing_created_at(package_path: Path | None) -> str:
    if package_path is None:
        return utc_now_iso()
    try:
        metadata = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return utc_now_iso()
    value = metadata.get("created_at") if isinstance(metadata, dict) else None
    return str(value) if value else utc_now_iso()


def _existing_package_satisfies_contract(package_path: Path, item: ContentItem) -> bool:
    try:
        metadata = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(metadata, dict) and package_metadata_satisfies_extraction_contract(
        metadata, item
    )


def _version_suffix(timestamp: str) -> str:
    return "v" + timestamp.replace("+00:00", "Z").replace(":", "").replace("-", "")


def _primary_source_map_entry(
    item: ContentItem,
    result: TranscriptResult,
    input_ref: str,
) -> dict[str, Any]:
    return {
        "target": "index.md#content",
        "source_ref": _tier1_source_ref(item, result, input_ref),
        "confidence": 1.0,
        "notes": [],
    }


def _ocr_source_map_entries(item: ContentItem, result: TranscriptResult, input_ref: str) -> list[dict[str, Any]]:
    """Project only numeric OCR locators; never persist an arbitrary backend payload."""
    if item.platform != "screenshot" or result.backend != "ocr.rapidocr":
        return []
    rows = result.metadata.get("source_map")
    if not isinstance(rows, list):
        return []
    entries = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("line")) is not int or row["line"] < 1:
            continue
        bbox = row.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(type(v) in (int, float) and math.isfinite(v) for v in bbox):
            continue
        source_ref = _tier1_source_ref(item, result, input_ref)
        source_ref["bbox"] = bbox
        entries.append({
            "target": "index.md#content",
            "source_ref": source_ref,
            "confidence": None,
            "notes": [f"OCR body line {row['line']}; pixel coordinates; reading order requires source verification."],
        })
    return entries


def _tier1_source_ref(
    item: ContentItem,
    result: TranscriptResult,
    input_ref: str,
) -> dict[str, Any]:
    source_ref = {
        "source_id": "src_001",
        "url": input_ref,
        "page": _locator_value(item, result, "page", "page_number", "page_index"),
        "timestamp": _locator_value(item, result, "timestamp", "start_timestamp", "start_time", "start"),
        "bbox": _locator_value(item, result, "bbox", "bounding_box"),
        "message_id": _locator_value(item, result, "message_id", "msg_id", "msgid", "mid"),
        "video_id": _video_locator(item, result),
        "episode_id": _episode_locator(item, result),
        "section_id": _section_locator(item, result),
    }
    return {key: _sanitize_locator(value) for key, value in source_ref.items()}


def _video_locator(item: ContentItem, result: TranscriptResult) -> Any:
    value = _locator_value(
        item,
        result,
        "video_id",
        "bvid",
        "bv_id",
        "aid",
        "aweme_id",
        "content_id",
    )
    if value is not None:
        return value
    if item.source_type == "video" and item.id:
        return item.id
    return None


def _episode_locator(item: ContentItem, result: TranscriptResult) -> Any:
    value = _locator_value(item, result, "episode_id", "episode", "eid")
    if value is not None:
        return value
    if item.platform == "xiaoyuzhou" and item.id:
        return item.id
    return None


def _section_locator(item: ContentItem, result: TranscriptResult) -> Any:
    return _locator_value(item, result, "section_id", "section", "chapter_id", "part_id")


def _locator_value(item: ContentItem, result: TranscriptResult, *keys: str) -> Any:
    for source in (result.metadata, item.raw_meta):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if _has_locator_value(value):
                return value
    return None


def _has_locator_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _sanitize_locator(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_error_summary(value.strip())
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [_sanitize_locator(item) for item in value[:20]]
    if isinstance(value, tuple):
        return [_sanitize_locator(item) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(sanitize_error_summary(str(key)) or ""): _sanitize_locator(child)
            for key, child in value.items()
            if _has_locator_value(child)
        }
    return sanitize_error_summary(str(value).strip())


def _permission_scope(item: ContentItem) -> str:
    if isinstance(item.raw_meta, dict):
        value = item.raw_meta.get("permission_scope")
        if isinstance(value, str):
            return value
    if item.url.startswith("file:"):
        return "local_file"
    if not item.url.startswith(("http://", "https://")):
        try:
            if Path(str(item.url)).exists():
                return "local_file"
        except OSError:
            pass
    return "unknown"


def _metadata_language(item: ContentItem, result: TranscriptResult) -> str:
    for source in (result.metadata, item.raw_meta if isinstance(item.raw_meta, dict) else {}):
        value = source.get("language")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _single_line_text(value: str) -> str:
    text = sanitize_url_text(str(value)).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())


def _yaml_scalar(value: str) -> str:
    return json.dumps(_single_line_text(value), ensure_ascii=False)
