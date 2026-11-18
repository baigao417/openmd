"""Read-only planning; required document executables may be checked locally."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openmd.core import plugins
from openmd.core.detector import DOCUMENT_EXTENSIONS, IMAGE_TEXT_EXTENSIONS, detect_platform
from openmd.backends.bilibili import (
    BILIBILI_OPENCLI_DISCOVERY_BACKEND,
    BILIBILI_OPENCLI_SUBTITLE_BACKEND,
    extract_bilibili_uid,
)
from openmd.backends.douyin import DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND, douyin_user_target
from openmd.backends.web_article import OPENCLI_WEB_READ_BACKEND
from openmd.backends.youtube import OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND
from openmd.backends.youtube_transcript import extract_youtube_video_id
from openmd.backends.youtube_discovery import (
    YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    YOUTUBE_DISCOVERY_BACKEND,
    YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
    normalize_youtube_collection_url,
    youtube_data_api_channel_lookup,
    youtube_data_api_playlist_id,
)

from .capabilities import CapabilityRecord, get_capability
from .operation_contract import COLLECTION_BINDINGS, collect_all_allowed


READ_ONLY_SIDE_EFFECTS = (
    "no_provider_execution",
    "no_network",
    "no_browser_or_session_access",
    "no_input_content_read",
    "no_media_or_model_download",
)
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac"}
_VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}


@dataclass(frozen=True)
class PlanResult:
    ok: bool
    detected_source: str
    input_kind: str
    capability_tier: str | None
    implementation_state: str | None
    source_exhausted_requested: bool
    source_exhausted_approved: bool
    planned_provider: str | None
    runtime_provider: str | None
    binding_state: str | None
    execution_ready: bool
    required_gates: tuple[str, ...]
    cost_class: str | None
    side_effects: tuple[str, ...]
    blocked_reason: str | None
    error_code: str | None
    discovery_provider: str | None = None
    required_backend: str | None = None
    required_discovery_backend: str | None = None
    next_action: str | None = None
    checked_engine: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detected_source": self.detected_source,
            "input_kind": self.input_kind,
            "capability_tier": self.capability_tier,
            "implementation_state": self.implementation_state,
            "source_exhausted_requested": self.source_exhausted_requested,
            "source_exhausted_approved": self.source_exhausted_approved,
            "planned_provider": self.planned_provider,
            "runtime_provider": self.runtime_provider,
            "binding_state": self.binding_state,
            "execution_ready": self.execution_ready,
            "required_gates": list(self.required_gates),
            "cost_class": self.cost_class,
            "side_effects": list(self.side_effects),
            "blocked_reason": self.blocked_reason,
            "error_code": self.error_code,
            "discovery_provider": self.discovery_provider,
            "required_backend": self.required_backend,
            "required_discovery_backend": self.required_discovery_backend,
            "next_action": self.next_action,
            "checked_engine": self.checked_engine,
            "readiness_scope": "local_document_dependency_checked" if self.checked_engine else "static_contract_only",
            "runtime_checks": "required local document executable checked; input content, models, credentials and source access are not probed" if self.checked_engine else "dependencies, models, credentials and source access are not probed",
        }


def _local_candidate(value: str) -> Path | None:
    """Resolve spelling only. Never expand a folder or touch an input body."""
    if _WINDOWS_PATH.match(value):
        return Path(value)
    parsed = urlparse(value)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("network_path_not_supported")
        raw = unquote(parsed.path)
        if raw.startswith(("\\\\", "//")):
            raise ValueError("network_path_not_supported")
        if re.match(r"^/[A-Za-z]:/", raw):
            raw = raw[1:]
        return Path(raw)
    if parsed.scheme:
        return None
    if value.startswith(("\\\\", "//")):
        raise ValueError("network_path_not_supported")
    return Path(value) if value else None


def _local_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _AUDIO_SUFFIXES:
        return "local_audio"
    if suffix in _VIDEO_SUFFIXES:
        return "local_video"
    if suffix in DOCUMENT_EXTENSIONS:
        return "pdf_office"
    if suffix in IMAGE_TEXT_EXTENSIONS:
        return "screenshot"
    return "unknown"


def _inspect_input(value: str) -> tuple[str, str, Path | None, str | None]:
    """Return source/shape/path/error using URL syntax or one local stat only."""
    if re.fullmatch(r"BV[0-9A-Za-z]+", value):
        return "bilibili", "single", None, None
    try:
        path = _local_candidate(value)
        if path is not None:
            source = _local_source(path)
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                return source, "file", path, "input_missing" if source != "unknown" else "source_unsupported"
            except OSError:
                return source, "file", path, "input_stat_failed"
            if stat.S_ISDIR(mode):
                return "unknown", "folder", path, "folder_source_not_inspected"
            if not stat.S_ISREG(mode):
                return source, "file", path, "input_not_regular_file"
            return source, "file", path, None
        parsed = urlparse(value)
        plugin_source = plugins.detect_platform(value)
        if plugin_source is not None:
            return plugin_source, _infer_remote_kind(value, plugin_source), None, None
        if parsed.scheme in {"http", "https"}:
            if not parsed.hostname or any(c.isspace() for c in value):
                return "unknown", "single", None, "source_unsupported"
            # Only HTTP URLs reach the legacy detector, so its local recursive
            # directory classification is unreachable from this projection.
            source = detect_platform(value)
            if source == "pdf":
                source = "pdf_office"
            if source == "xiaohongshu" and "/user/profile/" in parsed.path:
                source = "xiaohongshu_creator"
            return source, _infer_remote_kind(value, source), None, None
        if parsed.scheme in {"weixin", "wx-official", "wechat-official", "wechat", "wx", "qq", "qq-group", "qq_export", "qq-export"}:
            source = detect_platform(value)
            if source != "web_article":
                return source, "collection", None, None
    except (ValueError, OSError):
        return "unknown", "single", None, "source_unsupported"
    return "unknown", "single", None, "source_unsupported"


def _infer_remote_kind(value: str, source: str) -> str:
    parsed = urlparse(value)
    path = parsed.path.lower()
    if source == "youtube" and normalize_youtube_collection_url(value) is not None:
        return "collection"
    if source == "bilibili" and ((parsed.hostname or "").startswith("space.") or "/collection" in path or "/lists/" in path):
        return "collection"
    if source == "xiaoyuzhou" and "/podcast/" in path:
        return "collection"
    if source == "douyin" and ("/user/" in path or "sec_uid=" in parsed.query):
        return "collection"
    if source in {"weixin", "xiaoe"} and any(marker in path for marker in ("/author", "/collection", "/course", "/catalog", "/column")):
        return "collection"
    if source in {"xiaohongshu_creator", "qq_group"} or source in plugins.collection_sources():
        return "collection"
    return "single"


def _blocked(
    source_id: str,
    input_kind: str,
    request_all: bool,
    *,
    code: str,
    reason: str,
    record: CapabilityRecord | None = None,
) -> PlanResult:
    return PlanResult(
        ok=False,
        detected_source=source_id,
        input_kind=input_kind,
        capability_tier=record.tier if record else None,
        implementation_state=record.state if record else None,
        source_exhausted_requested=request_all,
        source_exhausted_approved=False,
        planned_provider=record.planned_provider if record else None,
        runtime_provider=record.runtime_provider if record else None,
        binding_state=record.binding_state if record else None,
        execution_ready=False,
        required_gates=record.required_gates if record else (),
        cost_class=record.cost_class if record else None,
        side_effects=READ_ONLY_SIDE_EFFECTS,
        blocked_reason=reason,
        error_code=code,
    )


def build_plan(
    input_value: str,
    *,
    input_kind: str = "auto",
    request_all: bool = False,
    available_gates: set[str] | None = None,
) -> PlanResult:
    value = input_value.strip()
    source_id, inferred_kind, local_path, input_error = _inspect_input(value)
    selected_kind = inferred_kind if input_kind == "auto" else input_kind
    if input_error:
        reasons = {
            "input_missing": "local input file does not exist",
            "input_stat_failed": "local input metadata could not be read",
            "input_not_regular_file": "local input is not a regular file",
            "folder_source_not_inspected": "folder contents are not inspected by plan; plan individual files",
        }
        return _blocked(source_id, selected_kind, request_all, code=input_error, reason=reasons.get(input_error, "input source is not approved"))
    try:
        record = get_capability(source_id)
    except ValueError:
        return _blocked(source_id, selected_kind, request_all, code="source_unsupported", reason="input source is not approved")

    if source_id == "ai_chat":
        from openmd.core.detector import ai_chat_site
        from openmd.backends.ai_chat import ai_chat_backend_for_site

        site = ai_chat_site(value)
        site_backend = ai_chat_backend_for_site(site or "")
        if site_backend and site not in {"chatgpt", "deepseek", "grok"}:
            return PlanResult(
                ok=True,
                detected_source=source_id,
                input_kind=selected_kind,
                capability_tier=record.tier,
                implementation_state="unimplemented",
                source_exhausted_requested=request_all,
                source_exhausted_approved=False,
                planned_provider=site_backend,
                runtime_provider=None,
                binding_state="unimplemented",
                execution_ready=False,
                required_gates=("browser", "account"),
                cost_class="unknown",
                side_effects=READ_ONLY_SIDE_EFFECTS,
                blocked_reason=f"AI chat site is not implemented: {site}",
                error_code="ai_chat_site_not_implemented",
                required_backend=site_backend,
                next_action=f"AI chat site not implemented: {site}.",
            )

    if local_path is not None and selected_kind != inferred_kind:
        return _blocked(source_id, selected_kind, request_all, code="input_shape_unsupported", reason="input kind does not match local input", record=record)
    if inferred_kind == "collection" and selected_kind == "single":
        return _blocked(source_id, selected_kind, request_all, code="input_shape_unsupported", reason="collection input cannot be planned as a single item", record=record)
    if selected_kind not in record.input_kinds:
        return _blocked(source_id, selected_kind, request_all, code="input_shape_unsupported", reason="input kind is not approved for this source", record=record)
    if record.state == "paused":
        return _blocked(source_id, selected_kind, request_all, code="capability_paused", reason="source is paused before provider execution", record=record)
    if request_all and selected_kind not in record.source_exhausted_kinds:
        return _blocked(source_id, selected_kind, True, code="all_not_approved", reason="source exhaustion is not approved for this input kind", record=record)

    gates = available_gates or set()
    required = list(record.required_gates)
    runtime_provider = record.runtime_provider
    if local_path is not None and source_id == "pdf_office":
        from openmd.core.detector import LOCAL_DOCUMENT_EXTENSIONS
        if local_path.suffix.lower() in LOCAL_DOCUMENT_EXTENSIONS:
            from openmd.routing.loader import load_catalog
            runtime_provider = "document.engines"
            required = list(load_catalog().capabilities[runtime_provider].get("gates", []))
    if not set(required) <= gates and source_id in {"youtube", "bilibili", "weixin", "web_article"}:
        # Project an allowed fallback from the same declarations as execution.
        # Planning stays static: do not construct or probe a provider.
        from openmd.routing.loader import effective_routes, load_catalog

        catalog = load_catalog()
        for route in effective_routes(source_id):
            candidate = next((backend for backend in route["chain"]
                              if set(catalog.capabilities[backend].get("gates", [])) <= gates), None)
            if candidate:
                runtime_provider = candidate
                required = list(catalog.capabilities[candidate].get("gates", []))
                break
    discovery_provider = None
    required_discovery_backend = None
    operation_block = None
    if selected_kind == "collection":
        binding = COLLECTION_BINDINGS.get(source_id)
        if binding is None:
            operation_block = "collection discovery is not implemented"
        else:
            discovery_provider, discovery_gates = binding
            if source_id == "bilibili" and "network" not in gates and "browser" in gates:
                discovery_provider = BILIBILI_OPENCLI_DISCOVERY_BACKEND
                discovery_gates = ("browser",)
            if source_id == "bilibili" and extract_bilibili_uid(value) is None:
                operation_block = "only Bilibili creator discovery is currently implemented"
            if source_id == "douyin" and not request_all and douyin_user_target(value) is None:
                operation_block = "bounded Douyin discovery requires a creator identifier"
            if source_id == "youtube":
                playlist = youtube_data_api_playlist_id(value) is not None
                if normalize_youtube_collection_url(value) is None:
                    operation_block = "input is not a supported YouTube collection"
                if "browser" in gates and ("network" not in gates or "account" not in gates):
                    discovery_provider = YOUTUBE_PLAYLIST_DISCOVERY_BACKEND if playlist else YOUTUBE_DISCOVERY_BACKEND
                    discovery_gates = ("browser",)
                elif playlist:
                    discovery_provider = YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND
                elif youtube_data_api_channel_lookup(value) is None:
                    operation_block = "this YouTube collection requires the browser discovery gate"
                required_discovery_backend = discovery_provider
            required.extend(discovery_gates)
            if request_all and source_id == "douyin":
                discovery_provider = DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND
                required_discovery_backend = discovery_provider
    elif selected_kind == "list":
        operation_block = "list contents are not inspected by plan; plan individual inputs"
    elif source_id == "douyin":
        operation_block = "standalone Douyin input resolution is not implemented; use creator collection"
    elif source_id == "youtube" and extract_youtube_video_id(value) is None:
        operation_block = "input does not identify a YouTube video"
    if request_all and not collect_all_allowed(source_id, browser_enabled=True, network_enabled=True, discovery_provider=discovery_provider):
        operation_block = "source exhaustion is not implemented for this operation"
    if local_path is None and source_id in {"pdf_office", "screenshot"}:
        operation_block = "this provider requires a local file"
    dependency_block = None
    dependency_next_action = None
    required_engine = None
    if local_path is not None and source_id == "pdf_office":
        suffix = local_path.suffix.lower().lstrip(".")
        required_engine = "soffice" if suffix in {"doc", "xls", "ppt", "odp", "ods"} else "calibre" if suffix in {"mobi", "azw3", "fb2"} else None
        if required_engine:
            from openmd.engines.tools import CalibreEngine, SofficeEngine
            engine = SofficeEngine() if required_engine == "soffice" else CalibreEngine()
            if not engine.available()["ok"]:
                dependency_block = f"engine_unavailable:{required_engine}"
                dependency_next_action = engine.install_hint
    missing_gates = tuple(dict.fromkeys(gate for gate in required if gate not in gates))
    execution_ready = record.binding_state == "implemented" and not missing_gates and record.state not in {"blocked", "paused"} and operation_block is None and dependency_block is None
    if dependency_block:
        blocked_reason = dependency_block
    elif operation_block:
        blocked_reason = operation_block
    elif record.binding_state == "unimplemented":
        blocked_reason = "runtime provider is not implemented"
    elif missing_gates:
        blocked_reason = f"missing required gate(s): {', '.join(missing_gates)}"
    elif record.state == "blocked":
        blocked_reason = "capability is blocked"
    else:
        blocked_reason = None
    # An executable route can differ from the default CLI primary. Surface the
    # exact override so callers never silently execute a placeholder instead.
    required_backend = None
    if source_id in {"local_audio", "local_video", "tutorial_video", "douyin", "youtube", "bilibili", "weixin", "web_article"} or runtime_provider != record.planned_provider:
        required_backend = runtime_provider
    return PlanResult(
        ok=True,
        detected_source=source_id,
        input_kind=selected_kind,
        capability_tier=record.tier,
        implementation_state=record.state,
        source_exhausted_requested=request_all,
        source_exhausted_approved=request_all and selected_kind in record.source_exhausted_kinds,
        planned_provider=record.planned_provider,
        runtime_provider=runtime_provider,
        binding_state=record.binding_state,
        execution_ready=execution_ready,
        required_gates=missing_gates,
        cost_class=record.cost_class,
        side_effects=READ_ONLY_SIDE_EFFECTS,
        blocked_reason=blocked_reason,
        error_code="engine_unavailable" if dependency_block else None,
        next_action=dependency_next_action,
        checked_engine=required_engine,
        discovery_provider=discovery_provider,
        required_backend=required_backend,
        required_discovery_backend=required_discovery_backend,
    )
