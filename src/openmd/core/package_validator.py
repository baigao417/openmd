"""Validator for Openmd V1 ``item.openmd/`` packages."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .package_contract import (
    PACKAGE_PROFILES,
    PACKAGE_SCHEMA_VERSION,
    PACKAGE_SOURCE_LEVEL_VALUES,
    PACKAGE_STATUS_VALUES,
    PERMISSION_SCOPE_VALUES,
    PROFILE_SECTIONS,
    REQUIRED_METADATA_FIELDS,
    REQUIRED_SOURCE_FIELDS,
)
from .models import COMPONENT_STATUS_VALUES, FAILURE_CLASS_VALUES
from .secrets import SENSITIVE_TEXT_PATTERNS, is_sensitive_key, is_sensitive_query_key


_SECRET_TEXT_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|set-cookie|bearer|"
    r"xsec[_-]?token|session[_-]?key|api[_-]?key)\b"
)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_TEXT_SCAN_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml", ".html", ".htm", ".har"}
_RAW_ARTIFACT_SUFFIXES = {
    ".aac",
    ".avi",
    ".db",
    ".dump",
    ".flac",
    ".har",
    ".htm",
    ".html",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".sqlite",
    ".sqlite3",
    ".wav",
    ".webm",
    ".zip",
}


@dataclass(frozen=True)
class PackageValidationResult:
    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class PackageValidationError(ValueError):
    pass


def validate_package(path: str | Path) -> PackageValidationResult:
    package_path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []

    if package_path.suffix != ".openmd":
        errors.append("package path must end with .openmd")
    if not package_path.exists() or not package_path.is_dir():
        errors.append("package path must exist and be a directory")
        return PackageValidationResult(package_path, errors, warnings)

    required_files = ("index.md", "metadata.json", "sources.json")
    for filename in required_files:
        if not (package_path / filename).is_file():
            errors.append(f"missing required file: {filename}")
    if not (package_path / "assets").is_dir():
        errors.append("missing required assets/ directory")
    if errors:
        return PackageValidationResult(package_path, errors, warnings)

    metadata = _read_json_object(package_path / "metadata.json", errors)
    sources_doc = _read_json_object(package_path / "sources.json", errors)
    index_text = _read_text(package_path / "index.md", errors)

    if metadata is not None:
        _validate_metadata(metadata, errors)
    if sources_doc is not None:
        _validate_sources(sources_doc, errors)
    source_ids = _source_ids(sources_doc)
    if metadata is not None and sources_doc is not None:
        _validate_primary_source(sources_doc, errors)
    if metadata is not None and index_text is not None:
        _validate_index(index_text, metadata, errors)

    _validate_assets(package_path, errors)
    _validate_optional_sidecars(package_path, metadata, source_ids, errors, warnings)
    _scan_for_secrets(package_path, errors)

    return PackageValidationResult(package_path, errors, warnings)


def validate_package_or_raise(path: str | Path) -> PackageValidationResult:
    result = validate_package(path)
    if not result.ok:
        raise PackageValidationError("; ".join(result.errors))
    return result


def _read_json_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path.name} is not valid JSON: {exc.msg}")
        return None
    except OSError as exc:
        errors.append(f"{path.name} could not be read: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path.name} must be a JSON object")
        return None
    return value


def _read_text(path: Path, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{path.name} could not be read: {exc}")
        return None


def _validate_metadata(metadata: dict[str, Any], errors: list[str]) -> None:
    _require_fields(metadata, REQUIRED_METADATA_FIELDS, "metadata.json", errors)
    if metadata.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        errors.append("metadata.json package_schema_version must be 1.0-draft")
    if metadata.get("profile") not in PACKAGE_PROFILES:
        errors.append("metadata.json profile is not allowed")
    if metadata.get("status") not in PACKAGE_STATUS_VALUES:
        errors.append("metadata.json status is not allowed")
    if metadata.get("source_level") not in PACKAGE_SOURCE_LEVEL_VALUES:
        errors.append("metadata.json source_level is not allowed")
    if not isinstance(metadata.get("modalities"), list):
        errors.append("metadata.json modalities must be an array")
    if not isinstance(metadata.get("warnings"), list):
        errors.append("metadata.json warnings must be an array")
    if not isinstance(metadata.get("backend"), dict):
        errors.append("metadata.json backend must be an object")
    _validate_extraction_components(metadata, errors)
    if "blocks" in metadata:
        _validate_blocks(metadata.get("blocks"), errors)


def _validate_blocks(blocks: Any, errors: list[str]) -> None:
    if not isinstance(blocks, list):
        errors.append("metadata.json blocks must be an array")
        return
    for index, block in enumerate(blocks):
        label = f"metadata.json blocks[{index}]"
        if not isinstance(block, dict):
            errors.append(f"{label} must be an object")
            continue
        kind = block.get("type")
        if kind == "turn":
            for field_name in ("role", "text"):
                if not isinstance(block.get(field_name), str):
                    errors.append(f"{label}.{field_name} must be a string")
        elif kind == "cue":
            for field_name in ("start", "end", "text"):
                if field_name not in block:
                    errors.append(f"{label} missing required field: {field_name}")
        elif kind == "table":
            if not isinstance(block.get("rows"), list):
                errors.append(f"{label}.rows must be an array")
        else:
            errors.append(f"{label}.type is not allowed")


def _validate_extraction_components(metadata: dict[str, Any], errors: list[str]) -> None:
    version = metadata.get("extraction_contract_version")
    components = metadata.get("components")
    if version is None and components is None:
        return
    if not isinstance(version, str) or not version.strip():
        errors.append("metadata.json extraction_contract_version must be a non-empty string")
    if not isinstance(components, list) or not components:
        errors.append("metadata.json components must be a non-empty array")
        return
    seen_ids: set[str] = set()
    for index, component in enumerate(components):
        prefix = f"metadata.json components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _require_fields(
            component,
            ("id", "kind", "status", "source_level", "backend", "text_ref", "warnings"),
            prefix,
            errors,
        )
        component_id = component.get("id")
        if not isinstance(component_id, str) or not component_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
        elif component_id in seen_ids:
            errors.append(f"{prefix}.id must be unique")
        else:
            seen_ids.add(component_id)
        if not isinstance(component.get("kind"), str) or not str(component.get("kind")).strip():
            errors.append(f"{prefix}.kind must be a non-empty string")
        if component.get("status") not in COMPONENT_STATUS_VALUES:
            errors.append(f"{prefix}.status is not allowed")
        if component.get("source_level") not in PACKAGE_SOURCE_LEVEL_VALUES:
            errors.append(f"{prefix}.source_level is not allowed")
        if not isinstance(component.get("backend"), str) or not str(component.get("backend")).strip():
            errors.append(f"{prefix}.backend must be a non-empty string")
        if not isinstance(component.get("text_ref"), str) or not str(component.get("text_ref")).strip():
            errors.append(f"{prefix}.text_ref must be a non-empty string")
        if not isinstance(component.get("warnings"), list):
            errors.append(f"{prefix}.warnings must be an array")
        failure_class = component.get("failure_class")
        if failure_class is not None and failure_class not in FAILURE_CLASS_VALUES:
            errors.append(f"{prefix}.failure_class is not allowed")
        if component.get("status") == "unavailable":
            for field_name in ("terminal_reason", "error_code", "failure_class"):
                value = component.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix}.{field_name} is required for unavailable components"
                    )
            if component.get("failure_class") != "source_unavailable":
                errors.append(
                    f"{prefix}.failure_class must be source_unavailable for unavailable components"
                )


def _validate_sources(sources_doc: dict[str, Any], errors: list[str]) -> None:
    if sources_doc.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        errors.append("sources.json package_schema_version must be 1.0-draft")
    sources = sources_doc.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources.json sources must be a non-empty array")
        return
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"sources.json sources[{index}] must be an object")
            continue
        _require_fields(source, REQUIRED_SOURCE_FIELDS, f"sources[{index}]", errors)
        if source.get("permission_scope") not in PERMISSION_SCOPE_VALUES:
            errors.append(f"sources.json sources[{index}].permission_scope is not allowed")
        if not isinstance(source.get("adapter"), dict):
            errors.append(f"sources.json sources[{index}].adapter must be an object")
        if not isinstance(source.get("source_map"), list):
            errors.append(f"sources.json sources[{index}].source_map must be an array")


def _validate_primary_source(sources_doc: dict[str, Any], errors: list[str]) -> None:
    primary = sources_doc.get("primary_source_id")
    source_ids = _source_ids(sources_doc)
    if not primary:
        errors.append("sources.json primary_source_id is required")
    elif primary not in source_ids:
        errors.append("sources.json primary_source_id must refer to sources[].source_id")


def _source_ids(sources_doc: dict[str, Any] | None) -> set[str]:
    if not isinstance(sources_doc, dict):
        return set()
    sources = sources_doc.get("sources")
    if not isinstance(sources, list):
        return set()
    return {
        str(source.get("source_id"))
        for source in sources
        if isinstance(source, dict) and source.get("source_id")
    }


def _validate_index(index_text: str, metadata: dict[str, Any], errors: list[str]) -> None:
    frontmatter = _FRONTMATTER_RE.match(index_text)
    if not frontmatter:
        errors.append("index.md must start with YAML frontmatter")
    else:
        _validate_frontmatter(frontmatter.group(1), metadata, errors)
    title = str(metadata.get("title") or "").strip()
    if title and f"# {title}" not in index_text:
        errors.append("index.md must contain # <Title>")
    if "## Source" not in index_text:
        errors.append("index.md must contain ## Source")
    profile = str(metadata.get("profile") or "")
    required_section = PROFILE_SECTIONS.get(profile)
    if required_section and f"## {required_section}" not in index_text:
        errors.append(f"index.md must contain ## {required_section}")
    if metadata.get("status") in {"verified", "partial"}:
        body = _FRONTMATTER_RE.sub("", index_text).strip()
        if not body or "_No extracted content._" in body:
            errors.append("index.md body must be non-empty for verified/partial packages")
    if metadata.get("status") == "failed" and "failed" not in index_text.lower():
        errors.append("failed packages must include a visible failure reason")


def _validate_frontmatter(frontmatter_text: str, metadata: dict[str, Any], errors: list[str]) -> None:
    values: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            errors.append(f"index.md frontmatter line is not key/value: {line}")
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key in values:
            errors.append(f"index.md frontmatter duplicate key: {key}")
            continue
        values[key] = _frontmatter_scalar_value(raw_value.strip())

    expected = {
        "title": str(metadata.get("title") or ""),
        "profile": str(metadata.get("profile") or ""),
        "source_platform": str(metadata.get("source_platform") or ""),
        "status": str(metadata.get("status") or ""),
        "source_level": str(metadata.get("source_level") or ""),
        "created_at": str(metadata.get("created_at") or ""),
    }
    for key, expected_value in expected.items():
        actual = values.get(key)
        if actual is None:
            errors.append(f"index.md frontmatter missing key: {key}")
        elif actual != expected_value:
            errors.append(f"index.md frontmatter {key} does not match metadata.json")


def _frontmatter_scalar_value(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
        return str(parsed)
    return value


def _validate_assets(package_path: Path, errors: list[str]) -> None:
    assets_path = package_path / "assets"
    if not assets_path.exists():
        return
    for path in assets_path.rglob("*"):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        relative_path = path.relative_to(package_path)
        suffix = path.suffix.lower()
        if suffix in _RAW_ARTIFACT_SUFFIXES:
            errors.append(f"raw/debug artifact is not allowed by default: {relative_path}")
        if any(is_sensitive_key(part) or is_sensitive_query_key(part) for part in path.parts):
            errors.append(f"credential-like asset path is not allowed: {relative_path}")


def _validate_optional_sidecars(
    package_path: Path,
    metadata: dict[str, Any] | None,
    source_ids: set[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    profile = str(metadata.get("profile") or "") if isinstance(metadata, dict) else ""
    allowed_sidecars = _allowed_sidecars_for_profile(profile)
    for path in package_path.iterdir():
        if path.name in {"index.md", "metadata.json", "sources.json", "assets"}:
            continue
        if not path.is_file():
            continue
        if path.name not in allowed_sidecars:
            errors.append(f"{path.name} is not allowed for profile: {profile or 'unknown'}")
            continue
        if path.suffix == ".json":
            value = _read_json_object(path, errors)
            if value is not None:
                _validate_json_sidecar(path.name, value, source_ids, errors)
        elif path.suffix == ".md" and path.name != "index.md":
            text = _read_text(path, errors)
            if text is not None and not text.strip():
                warnings.append(f"{path.name} is empty")
        else:
            errors.append(f"unsupported sidecar file: {path.name}")


def _allowed_sidecars_for_profile(profile: str) -> set[str]:
    return {
        "article": set(),
        "media": {"transcript.md", "timeline.json"},
        "conversation": {"messages.json"},
        "document": {"chunks.json"},
        "process": {"transcript.md", "timeline.json", "steps.json"},
    }.get(profile, set())


def _validate_json_sidecar(
    name: str,
    value: dict[str, Any],
    source_ids: set[str],
    errors: list[str],
) -> None:
    if name == "timeline.json":
        _validate_sidecar_items(
            name,
            value.get("items"),
            ("id", "start", "end", "text", "source_map"),
            source_ids,
            errors,
        )
    elif name == "messages.json":
        _validate_sidecar_items(
            name,
            value.get("messages"),
            ("id", "timestamp", "speaker", "text", "attachments", "links", "source_map"),
            source_ids,
            errors,
        )
    elif name == "steps.json":
        _validate_sidecar_items(
            name,
            value.get("steps"),
            ("id", "title", "text", "evidence_refs", "source_map"),
            source_ids,
            errors,
        )
    elif name == "chunks.json":
        _validate_sidecar_items(
            name,
            value.get("chunks"),
            ("id", "text", "source_map"),
            source_ids,
            errors,
        )


def _validate_sidecar_items(
    name: str,
    items: Any,
    required_fields: tuple[str, ...],
    source_ids: set[str],
    errors: list[str],
) -> None:
    if not isinstance(items, list) or not items:
        errors.append(f"{name} must contain a non-empty array")
        return
    for index, item in enumerate(items):
        label = f"{name}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        _require_fields(item, required_fields, label, errors)
        source_map = item.get("source_map")
        if not isinstance(source_map, list):
            errors.append(f"{label}.source_map must be an array")
            continue
        _validate_sidecar_source_map(name, index, source_map, source_ids, errors)


def _validate_sidecar_source_map(
    name: str,
    index: int,
    source_map: list[Any],
    source_ids: set[str],
    errors: list[str],
) -> None:
    for map_index, entry in enumerate(source_map):
        label = f"{name}[{index}].source_map[{map_index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        source_ref = entry.get("source_ref")
        if not isinstance(source_ref, dict):
            errors.append(f"{label}.source_ref must be an object")
            continue
        source_id = source_ref.get("source_id")
        if source_id and str(source_id) not in source_ids:
            errors.append(f"{label}.source_ref.source_id must refer to sources[].source_id")


def _scan_for_secrets(package_path: Path, errors: list[str]) -> None:
    for path in package_path.rglob("*"):
        if not path.is_file():
            continue
        if path.name == ".gitkeep":
            continue
        if path.suffix.lower() not in _TEXT_SCAN_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _SECRET_TEXT_RE.search(text) or any(pattern.search(text) for pattern in SENSITIVE_TEXT_PATTERNS):
            errors.append(f"possible secret-bearing text in {path.relative_to(package_path)}")


def _require_fields(
    value: dict[str, Any],
    required_fields: tuple[str, ...],
    label: str,
    errors: list[str],
) -> None:
    for field_name in required_fields:
        if field_name not in value:
            errors.append(f"{label} missing required field: {field_name}")
            continue
        field_value = value[field_name]
        if field_value is None:
            errors.append(f"{label} field must not be null: {field_name}")
        elif isinstance(field_value, str) and not field_value.strip():
            errors.append(f"{label} field must not be empty: {field_name}")
