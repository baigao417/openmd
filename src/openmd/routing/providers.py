"""Bridge declarative command cards into the existing backend executor."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendHealth
from openmd.core.local_config import load_local_config
from openmd.core.login import structured_login_code
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .loader import load_catalog, route_trace
from .readers import MEDIA_EXTENSIONS, read_media, read_text
from .runner import ToolError, isolated_environment, resolve_launcher, run_tool

from openmd.runtime.tempdirs import owned_temporary_directory

GATE_OPTIONS = {"network": "enable_network_backends", "browser": "enable_browser_backends", "media_download": "enable_media_download_backends", "asr": "enable_asr_backends", "cloud": "enable_cloud_backends"}


def gates_open(capability: dict[str, Any], options) -> bool:
    return all(gate not in {"account", "local_export"} and (gate == "local_compute" or bool(getattr(options, GATE_OPTIONS.get(gate, ""), False))) for gate in capability.get("gates", []))


def installed_identity(card: dict[str, Any]) -> dict[str, Any]:
    launcher = resolve_launcher(card)
    digest = hashlib.sha256(launcher.entry_path.read_bytes()).hexdigest()
    actual = None
    if card["kind"] == "command":
        for parent in [launcher.entry_path.parent, *list(launcher.entry_path.parents)[:3]]:
            metadata = parent / "package.json"
            if metadata.is_file():
                try:
                    actual = json.loads(metadata.read_text(encoding="utf-8")).get("version")
                except (OSError, ValueError):
                    pass
                if actual:
                    break
    elif card["kind"] == "python_entry":
        paths = load_local_config().get("tools", {}).get(card["id"], {}).get("paths")
        wanted = card["id"].lower().replace("_", "-")
        for dist in importlib.metadata.distributions(path=paths):
            if str(dist.metadata.get("Name", "")).lower().replace("_", "-") == wanted:
                actual = dist.version
                break
    if card["kind"] == "python_script":
        actual = "sha256:" + digest
        verified = card.get("installation", {}).get("sha256") == digest
    else:
        verified = actual == card.get("version")
    locked_digest = card.get("installation", {}).get("sha256")
    if locked_digest and locked_digest != digest:
        verified = False
    return {"tool_version": actual, "tool_version_verified": verified, "version_origin": "installed_metadata" if card["kind"] != "python_script" else "source_sha256", "entry_sha256": digest, "origin": launcher.origin}


class CommandToolProvider:
    def __init__(self, card: dict[str, Any], capability: dict[str, Any], options) -> None:
        self.card, self.capability, self.options = card, capability, options
        self.name = capability["id"]

    def descriptor(self) -> BackendDescriptor:
        gates = self.capability.get("gates", [])
        return BackendDescriptor(name=self.name, layer="command_card", capabilities=(BackendCapability(platforms=tuple(self.capability["sources"]), source_types=("article", "audio", "video"), input_types=("single", "batch", "collection"), source_levels=(self.capability["source_level"],), requires_network="network" in gates, requires_credentials=bool(self.capability.get("requires_login"))),), description="Declared tool with validated artifact output.", provider=self.card["id"])

    def health(self) -> BackendHealth:
        if not gates_open(self.capability, self.options):
            return BackendHealth(ok=False, error_code="tool_permission_required", error_summary="Explicit runtime gates are required for this tool.")
        try:
            identity = installed_identity(self.card)
            if not identity["tool_version_verified"]:
                return BackendHealth(ok=False, error_code="tool_version_mismatch", error_summary="Installed tool identity differs from its locked card; inspect doctor before updating the card.")
            return BackendHealth(ok=True, dependencies={self.card["id"]: identity["tool_version"]})
        except (ToolError, OSError, ValueError) as exc:
            return BackendHealth(ok=False, error_code=getattr(exc, "code", "tool_not_installed"), error_summary=sanitize_error_summary(exc))

    def extract(self, request) -> TranscriptResult:
        if not gates_open(self.capability, self.options):
            return self._failed("tool_permission_required", "Required runtime gates have not been enabled.")
        if request.item.platform not in self.capability["sources"]:
            return self._failed("tool_input_unsupported", "Tool card does not cover this source.")
        metadata: dict[str, Any] = {}
        try:
            identity = installed_identity(self.card)
            if not identity["tool_version_verified"]:
                return self._failed("tool_version_mismatch", "Installed tool differs from its locked card; run openmd doctor --profile tools.")
            with owned_temporary_directory(prefix="openmd-tool-", ignore_cleanup_errors=True) as tmp:
                workdir = Path(tmp)
                run = run_tool(self.card, self.capability, request.item, workdir)
                metadata = {"tool_run": {"seconds": run.seconds, "exit_code": run.exit_code, "files": run.files, "card_id": self.card["id"], **identity}, **route_trace(request.item.platform, self.name)}
                if self.card["id"] == "yutto":
                    metadata["tool_run"]["anonymous_notice"] = "未提供登录认证信息" in run.stdout + run.stderr
                login_code = structured_login_code(run.stdout)
                if login_code or run.exit_code == 77:
                    return replace(self._failed(login_code or "backend_exit_77", "Tool requires login."), metadata=metadata)
                if run.exit_code != 0:
                    return replace(self._failed("tool_backend_exit", f"Tool exited with code {run.exit_code}; raw logs were not retained."), metadata=metadata)
                if self.capability.get("then"):
                    from openmd.runtime.asr import get_asr_backend

                    media = read_media(workdir)
                    result = get_asr_backend(self.capability["then"]).transcribe(media)
                    metadata.update({"media_tool": self.card["id"], "media_bytes": media.stat().st_size, **result.metadata})
                    result = replace(result, backend=self.name, source_level="media_asr" if result.has_usable_text else "partial_extraction", title=request.item.title or media.stem, source_notes=[*result.source_notes, f"Media fetched by {self.card['id']} and transcribed locally; command logs are not transcript evidence."])
                    if getattr(self.options, "keep_media_artifacts", False) and getattr(self.options, "media_artifact_dir", None):
                        destination = Path(self.options.media_artifact_dir)
                        try:
                            destination.mkdir(parents=True, exist_ok=True)
                            with tempfile.NamedTemporaryFile(prefix=hashlib.sha256(request.item.url.encode()).hexdigest()[:12] + "-", suffix=media.suffix, dir=destination, delete=False) as dst, media.open("rb") as src:
                                shutil.copyfileobj(src, dst)
                            metadata["media_retained"] = True
                        except OSError:
                            metadata["media_retained"] = False
                            result = replace(result, source_notes=[*result.source_notes, "Transcript succeeded, but the optional media copy could not be retained."])
                else:
                    result = read_text(self.capability, run, workdir)
                return replace(result, metadata={**result.metadata, **metadata})
        except (ToolError, OSError, ValueError) as exc:
            return replace(self._failed(getattr(exc, "code", "tool_artifact_invalid"), sanitize_error_summary(exc) or "Tool failed."), metadata=metadata)

    def download_only(self, item: ContentItem, destination: Path, *, media_kind: str) -> Path | TranscriptResult:
        """Run this declared media route and retain its media without invoking ASR."""
        download_capability = {**self.capability, "gates": [gate for gate in self.capability.get("gates", []) if gate != "asr"]}
        if self.capability.get("capability") != "media" or not gates_open(download_capability, self.options):
            return self._failed("tool_permission_required", "Network and media download permissions are required.")
        if item.platform not in self.capability.get("sources", ()):
            return self._failed("tool_input_unsupported", "Tool card does not cover this source.")
        try:
            identity = installed_identity(self.card)
            if not identity["tool_version_verified"]:
                return self._failed("tool_version_mismatch", "Installed tool differs from its locked card; run openmd doctor --profile tools.")
            capability = dict(self.capability)
            if self.card.get("id") == "yutto" and media_kind == "video":
                capability["argv"] = [arg for arg in capability.get("argv", []) if arg != "--audio-only"]
            with owned_temporary_directory(prefix="openmd-media-only-") as tmp:
                workdir = Path(tmp)
                run = run_tool(self.card, capability, item, workdir)
                login_code = structured_login_code(run.stdout)
                if login_code or run.exit_code == 77:
                    return self._failed(login_code or "backend_exit_77", "Tool requires login.")
                if run.exit_code != 0:
                    return self._failed("tool_backend_exit", f"Tool exited with code {run.exit_code}; raw logs were not retained.")
                candidates = [path for path in workdir.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and path.stat().st_size > 0 and path.resolve().is_relative_to(workdir.resolve())]
                preferred_suffixes = {"video": {".mp4", ".webm"}, "audio": {".m4a", ".mp3", ".aac", ".opus", ".wav", ".ogg", ".flac"}}
                preferred = [path for path in candidates if path.suffix.lower() in preferred_suffixes.get(media_kind, set())]
                media = max(preferred or candidates, key=lambda path: path.stat().st_size) if candidates else read_media(workdir)
                destination.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha256(item.url.encode("utf-8")).hexdigest()[:12] + media.suffix.lower()
                target = destination / name
                with media.open("rb") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                return target
        except (ToolError, OSError, ValueError) as exc:
            return self._failed(getattr(exc, "code", "tool_artifact_invalid"), sanitize_error_summary(exc) or "Tool failed.")

    def _failed(self, code: str, detail: str) -> TranscriptResult:
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=self.name, source_notes=["The declared tool did not produce validated source content."], error_code=code, error_summary=detail)


def register_card_providers(registry, platform: str, options) -> None:
    catalog = load_catalog()
    requested = options.backend_override
    for capability in catalog.capabilities.values():
        card = catalog.cards[capability["card_id"]]
        if card["kind"] != "builtin" and platform in capability["sources"] and (requested is None or requested == capability["id"]) and gates_open(capability, options):
            registry.register(capability["id"], CommandToolProvider(card, capability, options))


def doctor_tools(*, probe_commands: bool = False) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for card in load_catalog().cards.values():
        if card["kind"] == "builtin":
            continue
        record = {"tool": card["id"], "kind": card["kind"], "declared_version": card["version"], "requires_login": any(c.get("requires_login", False) for c in card["capabilities"]), "gates": sorted({g for c in card["capabilities"] for g in c.get("gates", [])}), "resolved": False, "origin": "none", "version": None, "version_probe": "not_requested"}
        try:
            identity = installed_identity(card)
            record.update(identity)
            record.update({"resolved": True, "version": identity["tool_version"]})
            if not identity["tool_version_verified"]:
                record["error_code"] = "tool_version_mismatch"
            # Local metadata/hash inspection is sufficient for these cards; never
            # run a tool's ordinary operation just to discover a version.
            if probe_commands:
                record["version_probe"] = "installed_metadata_and_source_hash"
        except (ToolError, OSError, ValueError) as exc:
            record["error_code"] = getattr(exc, "code", "tool_not_installed")
        reports.append(record)
    return {"ok": all(r["resolved"] and r.get("tool_version_verified") for r in reports), "tools": reports, "network_access": "not_probed", "private_session_state": "not_read"}
