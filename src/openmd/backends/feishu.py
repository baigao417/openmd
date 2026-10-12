"""Feishu/Lark document backend providers.

The providers in this module are registered only through the runtime extension
hook in ``openmd.backends.registry``. The lark-cli based providers stay behind
``--enable-network-backends`` because they may perform authenticated network
requests. The manual fallback is local-only and never invokes external tools.
"""

from __future__ import annotations

import json
import inspect
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary


LARK_CLI_DOCS_FETCH_BACKEND = "lark-cli.docs.fetch"
LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND = "lark-cli.drive.export-markdown"
LARK_CLI_MARKDOWN_FETCH_BACKEND = "lark-cli.markdown.fetch"
MANUAL_MARKDOWN_EXPORT_BACKEND = "manual.markdown-export"

SUPPORTED_DOC_TYPES: frozenset[str] = frozenset({"doc", "docx"})
UNSUPPORTED_V0_OBJECT_TYPES: frozenset[str] = frozenset(
    {"bitable", "sheet", "sheets", "slides", "mindnote", "folder", "file"}
)
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MIN_MARKDOWN_CHARS = 1

CommandRunner = Callable[..., tuple[int, str, str]]


@dataclass(frozen=True)
class FeishuObjectInfo:
    object_type: str | None = None
    title: str | None = None
    token: str | None = None
    file_name: str | None = None


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Feishu providers through explicit runtime gates."""
    requested = options.backend_override
    if options.enable_network_backends and requested in {None, LARK_CLI_DOCS_FETCH_BACKEND}:
        registry.register(LARK_CLI_DOCS_FETCH_BACKEND, LarkCliDocsFetchProvider())
    if options.enable_network_backends and requested in {None, LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND}:
        registry.register(
            LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND,
            LarkCliDriveExportMarkdownProvider(),
        )
    if options.enable_network_backends and requested in {None, LARK_CLI_MARKDOWN_FETCH_BACKEND}:
        registry.register(LARK_CLI_MARKDOWN_FETCH_BACKEND, LarkCliMarkdownFetchProvider())

    if requested in {None, MANUAL_MARKDOWN_EXPORT_BACKEND}:
        registry.register(MANUAL_MARKDOWN_EXPORT_BACKEND, ManualMarkdownExportProvider())


def build_lark_docs_fetch_args(doc: str) -> list[str]:
    return [
        "lark-cli",
        "docs",
        "+fetch",
        "--api-version",
        "v2",
        "--doc",
        doc,
        "--doc-format",
        "markdown",
        "--detail",
        "simple",
        "--format",
        "json",
    ]


def build_lark_drive_inspect_args(input_value: str) -> list[str]:
    return ["lark-cli", "drive", "+inspect", "--url", input_value, "--format", "json"]


def build_lark_drive_export_markdown_args(
    *,
    token: str,
    doc_type: str,
    output_dir: str,
) -> list[str]:
    return [
        "lark-cli",
        "drive",
        "+export",
        "--doc-type",
        doc_type,
        "--file-extension",
        "markdown",
        "--token",
        token,
        "--output-dir",
        output_dir,
        "--overwrite",
        "--format",
        "json",
    ]


def build_lark_markdown_fetch_args(file_token: str) -> list[str]:
    return ["lark-cli", "markdown", "+fetch", "--file-token", file_token, "--format", "json"]


class LarkCliDocsFetchProvider:
    """Fetch Feishu doc/docx native Markdown through ``lark-cli docs +fetch``."""

    name = LARK_CLI_DOCS_FETCH_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        min_markdown_chars: int = DEFAULT_MIN_MARKDOWN_CHARS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout
        self.min_markdown_chars = max(0, min_markdown_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("feishu",),
            source_types=("document",),
            input_types=("single", "batch"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=True,
            optional_config=("lark-cli", "lark-cli auth login"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor",
            capabilities=(capability,),
            description="Fetch Feishu doc/docx content as native Markdown through lark-cli docs +fetch.",
            provider="lark-cli",
        )

    def health(self) -> BackendHealth:
        return _lark_cli_health(self._runner)

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "feishu":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Feishu docs provider only supports feishu, got {item.platform}.",
            )

        doc_input = _preferred_doc_input(item)
        title = item.title
        object_type: str | None = None
        inspected = False

        if _should_inspect_before_docs_fetch(doc_input):
            inspected = True
            inspect_result = _inspect_feishu_object(
                doc_input,
                runner=self._runner,
                timeout=self.timeout,
                backend=self.name,
            )
            if isinstance(inspect_result, TranscriptResult):
                return inspect_result
            object_type = _normalize_object_type(inspect_result.object_type)
            title = inspect_result.title or title
            if object_type not in SUPPORTED_DOC_TYPES:
                return _unsupported_feishu_object(self.name, object_type, title=title)
            doc_input = inspect_result.token or doc_input

        exit_code, stdout, stderr = self._runner(
            build_lark_docs_fetch_args(doc_input),
            timeout=self.timeout,
        )
        if exit_code != 0:
            return _command_failed_result(
                self.name,
                exit_code,
                stdout,
                stderr,
                action="lark-cli docs +fetch",
            )

        payload = _loads_json(stdout)
        if payload is None:
            return _failed(self.name, "backend_invalid_json", "lark-cli docs +fetch returned invalid JSON.")
        if _payload_is_failed(payload):
            return _failed(self.name, "lark_cli_docs_fetch_failed", "lark-cli docs +fetch returned ok=false.")

        text = _extract_content(payload).strip()
        if len("".join(text.split())) < self.min_markdown_chars:
            return _failed(self.name, "empty_result", "lark-cli docs +fetch returned no Markdown content.")

        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            title=title or _extract_title(payload),
            author=item.author,
            published_at=item.published_at,
            source_notes=[
                "Feishu native Markdown fetched through lark-cli docs +fetch.",
                "No cookies, tokens, or raw API response were persisted by Openmd.",
            ],
            metadata={
                "strategy": "docs_fetch",
                "inspected": inspected,
                "object_type": object_type,
            },
        )


class LarkCliDriveExportMarkdownProvider:
    """Export Feishu doc/docx to a temporary Markdown file through lark-cli."""

    name = LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        min_markdown_chars: int = DEFAULT_MIN_MARKDOWN_CHARS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout
        self.min_markdown_chars = max(0, min_markdown_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("feishu",),
            source_types=("document",),
            input_types=("single", "batch"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=True,
            optional_config=("lark-cli", "temporary export directory"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor fallback",
            capabilities=(capability,),
            description="Fallback export of Feishu doc/docx to Markdown through lark-cli drive +export.",
            provider="lark-cli",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return _lark_cli_health(self._runner)

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "feishu":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Feishu drive export provider only supports feishu, got {item.platform}.",
            )

        inspect_result = _inspect_feishu_object(
            _preferred_doc_input(item),
            runner=self._runner,
            timeout=self.timeout,
            backend=self.name,
            fallback_info=_info_from_item(item),
        )
        if isinstance(inspect_result, TranscriptResult):
            return inspect_result

        object_type = _normalize_object_type(inspect_result.object_type)
        if object_type not in SUPPORTED_DOC_TYPES:
            return _unsupported_feishu_object(self.name, object_type, title=inspect_result.title or item.title)
        if not inspect_result.token:
            return _failed(self.name, "invalid_input", "Could not resolve a Feishu doc/docx token for export.")

        with tempfile.TemporaryDirectory(prefix="openmd-feishu-") as tmpdir:
            exit_code, stdout, stderr = _run_command_with_optional_cwd(
                self._runner,
                build_lark_drive_export_markdown_args(
                    token=inspect_result.token,
                    doc_type=object_type,
                    output_dir=".",
                ),
                timeout=self.timeout,
                cwd=tmpdir,
            )
            if exit_code != 0:
                return _command_failed_result(
                    self.name,
                    exit_code,
                    stdout,
                    stderr,
                    action="lark-cli drive +export",
                )

            payload = _loads_json(stdout)
            text = _read_exported_markdown(Path(tmpdir), payload).strip()
            if len("".join(text.split())) < self.min_markdown_chars:
                return _failed(
                    self.name,
                    "empty_result",
                    "lark-cli drive +export did not produce a usable Markdown file.",
                )
            result = TranscriptResult(
                text=text.rstrip() + "\n",
                status="verified",
                source_level="native_text",
                backend=self.name,
                title=inspect_result.title or item.title,
                author=item.author,
                published_at=item.published_at,
                source_notes=[
                    "Feishu doc/docx was exported to Markdown through lark-cli drive +export.",
                    "Openmd read the temporary Markdown file and deleted the temporary export directory.",
                    "No cookies, tokens, raw command streams, or raw API response were persisted by Openmd.",
                ],
                metadata={"strategy": "drive_export_markdown", "object_type": object_type},
            )
        return result


class LarkCliMarkdownFetchProvider:
    """Fetch Drive-native Markdown files through ``lark-cli markdown +fetch``."""

    name = LARK_CLI_MARKDOWN_FETCH_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        min_markdown_chars: int = DEFAULT_MIN_MARKDOWN_CHARS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout
        self.min_markdown_chars = max(0, min_markdown_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("feishu",),
            source_types=("document",),
            input_types=("single", "batch"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=True,
            optional_config=("lark-cli", "Drive-native Markdown file token"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor fallback",
            capabilities=(capability,),
            description="Fetch Drive-native Markdown file content through lark-cli markdown +fetch.",
            provider="lark-cli",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return _lark_cli_health(self._runner)

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "feishu":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Feishu Markdown provider only supports feishu, got {item.platform}.",
            )

        token = _markdown_token_candidate(item)
        title = item.title
        if token is None and _looks_like_feishu_url(item.url):
            inspect_result = _inspect_feishu_object(
                item.url,
                runner=self._runner,
                timeout=self.timeout,
                backend=self.name,
            )
            if isinstance(inspect_result, TranscriptResult):
                return inspect_result
            object_type = _normalize_object_type(inspect_result.object_type)
            title = inspect_result.title or inspect_result.file_name or title
            if object_type != "file" or not _is_markdown_file_name(title):
                return _unsupported_feishu_object(self.name, object_type, title=title)
            token = inspect_result.token

        if token is None:
            return _failed(
                self.name,
                "invalid_input",
                "Expected a Drive-native Markdown file token or inspectable Feishu Markdown file URL.",
            )

        exit_code, stdout, stderr = self._runner(build_lark_markdown_fetch_args(token), timeout=self.timeout)
        if exit_code != 0:
            return _command_failed_result(
                self.name,
                exit_code,
                stdout,
                stderr,
                action="lark-cli markdown +fetch",
            )

        payload = _loads_json(stdout)
        if payload is None:
            return _failed(self.name, "backend_invalid_json", "lark-cli markdown +fetch returned invalid JSON.")
        if _payload_is_failed(payload):
            return _failed(self.name, "lark_cli_markdown_fetch_failed", "lark-cli markdown +fetch returned ok=false.")

        text = _extract_content(payload).strip()
        if len("".join(text.split())) < self.min_markdown_chars:
            return _failed(self.name, "empty_result", "lark-cli markdown +fetch returned no Markdown content.")

        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            title=title or _extract_title(payload),
            author=item.author,
            published_at=item.published_at,
            source_notes=[
                "Drive-native Markdown fetched through lark-cli markdown +fetch.",
                "No cookies, tokens, or raw API response were persisted by Openmd.",
            ],
            metadata={"strategy": "markdown_fetch"},
        )


class ManualMarkdownExportProvider:
    """Local-only guidance for manually exported Feishu Markdown."""

    name = MANUAL_MARKDOWN_EXPORT_BACKEND

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("feishu",),
            source_types=("document",),
            input_types=("single", "batch"),
            source_levels=("partial_extraction",),
            requires_network=False,
            requires_credentials=False,
        )
        return BackendDescriptor(
            name=self.name,
            layer="manual fallback",
            capabilities=(capability,),
            description="Explain that the user can manually export Feishu Markdown and ingest it separately.",
            provider="openmd.manual",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={"manual": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=self.name,
            source_notes=[
                "Manual Feishu Markdown export is required for this input.",
                "Export or download Markdown from Feishu, then ingest the local Markdown artifact in a separate workflow.",
                "No network, browser automation, cloud API, or lark-cli command was attempted by this provider.",
            ],
            error_code="manual_markdown_export_required",
            error_summary="Manual Markdown export is required for this Feishu input.",
            metadata={"strategy": "manual_markdown_export"},
        )


def _run_command(
    args: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    actual_args = list(args)
    if actual_args and actual_args[0] == "lark-cli":
        actual_args[0] = _resolve_lark_cli_executable()

    try:
        completed = subprocess.run(
            actual_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "lark-cli executable is not available on PATH."
    except subprocess.TimeoutExpired:
        return 124, "", f"lark-cli command timed out after {timeout} seconds."
    return completed.returncode, completed.stdout, completed.stderr


def _resolve_lark_cli_executable() -> str:
    return shutil.which("lark-cli.cmd") or shutil.which("lark-cli") or "lark-cli"


def _lark_cli_health(runner: CommandRunner) -> BackendHealth:
    if runner is not _run_command:
        return BackendHealth(ok=True, dependencies={"lark-cli": "injected"})
    if shutil.which("lark-cli") is None and shutil.which("lark-cli.cmd") is None:
        return BackendHealth(
            ok=False,
            dependencies={"lark-cli": "missing"},
            error_code="backend_dependency_missing",
            error_summary="lark-cli executable is not available on PATH.",
        )
    return BackendHealth(ok=True, dependencies={"lark-cli": "available"})


def _runner_accepts_cwd(runner: CommandRunner) -> bool:
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or name == "cwd"
        for name, parameter in signature.parameters.items()
    )


def _run_command_with_optional_cwd(
    runner: CommandRunner,
    args: list[str],
    *,
    timeout: int,
    cwd: str | Path | None,
) -> tuple[int, str, str]:
    if cwd is not None and _runner_accepts_cwd(runner):
        return runner(args, timeout=timeout, cwd=cwd)
    return runner(args, timeout=timeout)


def _preferred_doc_input(item: ContentItem) -> str:
    raw_token = item.raw_meta.get("token") or item.raw_meta.get("doc_token") or item.raw_meta.get("file_token")
    return str(raw_token or item.id or item.url)


def _markdown_token_candidate(item: ContentItem) -> str | None:
    raw_token = item.raw_meta.get("file_token") or item.raw_meta.get("markdown_file_token") or item.raw_meta.get("token")
    if raw_token:
        return str(raw_token)
    if item.id:
        return item.id
    if not _looks_like_feishu_url(item.url) and "/" not in item.url and ":" not in item.url:
        return item.url.strip() or None
    return None


def _should_inspect_before_docs_fetch(input_value: str) -> bool:
    if not _looks_like_feishu_url(input_value):
        return False
    parsed = urlparse(input_value)
    path = parsed.path.lower()
    if "/wiki/" in path:
        return True
    first_part = next((part for part in path.split("/") if part), "")
    return first_part not in {"doc", "docx"}


def _looks_like_feishu_url(input_value: str) -> bool:
    if "://" not in input_value:
        return False
    parsed = urlparse(input_value)
    host = parsed.netloc.lower()
    return host.endswith("feishu.cn") or host.endswith("larksuite.com")


def _info_from_item(item: ContentItem) -> FeishuObjectInfo | None:
    object_type = item.raw_meta.get("type") or item.raw_meta.get("obj_type") or item.raw_meta.get("object_type")
    token = item.raw_meta.get("token") or item.raw_meta.get("obj_token") or item.raw_meta.get("file_token")
    title = item.raw_meta.get("title") or item.title
    if not object_type and not token and not title:
        return None
    return FeishuObjectInfo(
        object_type=str(object_type) if object_type else None,
        token=str(token) if token else None,
        title=str(title) if title else None,
    )


def _inspect_feishu_object(
    input_value: str,
    *,
    runner: CommandRunner,
    timeout: int,
    backend: str,
    fallback_info: FeishuObjectInfo | None = None,
) -> FeishuObjectInfo | TranscriptResult:
    if fallback_info is not None and fallback_info.object_type and fallback_info.token:
        return fallback_info

    exit_code, stdout, stderr = runner(build_lark_drive_inspect_args(input_value), timeout=timeout)
    if exit_code != 0:
        return _command_failed_result(backend, exit_code, stdout, stderr, action="lark-cli drive +inspect")

    payload = _loads_json(stdout)
    if payload is None:
        return _failed(backend, "backend_invalid_json", "lark-cli drive +inspect returned invalid JSON.")
    if _payload_is_failed(payload):
        return _failed(backend, "lark_cli_drive_inspect_failed", "lark-cli drive +inspect returned ok=false.")
    return _parse_object_info(payload)


def _parse_object_info(payload: Any) -> FeishuObjectInfo:
    data = _payload_data(payload)
    wiki_node = data.get("wiki_node") if isinstance(data, dict) and isinstance(data.get("wiki_node"), dict) else {}
    return FeishuObjectInfo(
        object_type=_first_string(data, ("type", "obj_type", "doc_type"))
        or _first_string(wiki_node, ("obj_type", "type")),
        token=_first_string(data, ("token", "obj_token", "file_token", "document_id"))
        or _first_string(wiki_node, ("obj_token", "token", "file_token")),
        title=_first_string(data, ("title", "name", "file_name")) or _first_string(wiki_node, ("title", "name")),
        file_name=_first_string(data, ("file_name", "name", "title")),
    )


def _normalize_object_type(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered == "sheets":
        return "sheet"
    return lowered or None


def _unsupported_feishu_object(backend: str, object_type: str | None, *, title: str | None = None) -> TranscriptResult:
    del title
    safe_type = object_type or "unknown"
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "Feishu object type is outside Openmd V0 document extraction scope.",
            "V0 only handles doc/docx native Markdown or Drive-native Markdown files.",
            "Bitable, sheet, slides, mindnote, folder, and non-Markdown files are not extracted as documents.",
        ],
        error_code="unsupported_feishu_object",
        error_summary=sanitize_error_summary(f"Unsupported Feishu object type: {safe_type}."),
        metadata={"object_type": safe_type},
    )


def _command_failed_result(
    backend: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    action: str,
) -> TranscriptResult:
    code = "backend_dependency_missing" if _looks_like_missing_lark_cli(exit_code, stdout, stderr) else f"backend_exit_{exit_code}"
    summary = "lark-cli executable is not available on PATH." if code == "backend_dependency_missing" else f"{action} failed with exit code {exit_code}."
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            f"{action} returned structured failure.",
            "Openmd stored only the exit code and a sanitized summary, not raw command output.",
        ],
        error_code=code,
        error_summary=sanitize_error_summary(summary),
        metadata={"exit_code": exit_code},
    )


def _looks_like_missing_lark_cli(exit_code: int, stdout: str, stderr: str) -> bool:
    text = f"{stdout} {stderr}".lower()
    return (
        exit_code in {127, 9009}
        or "enoent" in text
        or "not recognized" in text
        or "command not found" in text
        or "lark-cli: not found" in text
        or "executable is not available" in text
    )


def _failed(backend: str, code: str, error: object) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=["Feishu backend returned structured failure."],
        error_code=code,
        error_summary=sanitize_error_summary(error),
    )


def _loads_json(stdout: str) -> Any | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


def _payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _payload_is_failed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is False or payload.get("success") is False


def _extract_content(payload: Any) -> str:
    data = _payload_data(payload)
    candidates: list[Any] = []
    if isinstance(data, dict):
        for key in ("document", "doc", "file", "markdown"):
            value = data.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        candidates.append(data)
    if isinstance(payload, dict):
        candidates.append(payload)

    for candidate in candidates:
        value = _first_string(candidate, ("content", "markdown", "text", "body"))
        if value and value.strip():
            return value
    return ""


def _extract_title(payload: Any) -> str | None:
    data = _payload_data(payload)
    candidates: list[Any] = []
    if isinstance(data, dict):
        for key in ("document", "doc", "file"):
            value = data.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        candidates.append(data)
    if isinstance(payload, dict):
        candidates.append(payload)
    for candidate in candidates:
        title = _first_string(candidate, ("title", "file_name", "name"))
        if title:
            return title
    return None


def _first_string(mapping: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_exported_markdown(tmpdir: Path, payload: Any | None) -> str:
    for path in _exported_path_candidates(tmpdir, payload):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        if text.strip():
            return text
    return ""


def _exported_path_candidates(tmpdir: Path, payload: Any | None) -> list[Path]:
    candidates: list[Path] = []
    for value in _path_strings(payload):
        path = Path(value)
        if not path.is_absolute():
            path = tmpdir / path
        try:
            path.resolve().relative_to(tmpdir.resolve())
        except ValueError:
            continue
        candidates.append(path)

    candidates.extend(sorted(tmpdir.rglob("*.md")))
    candidates.extend(sorted(tmpdir.rglob("*.markdown")))
    candidates.extend(path for path in sorted(tmpdir.rglob("*")) if path.is_file())

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(path)
    return deduped


def _path_strings(payload: Any | None) -> list[str]:
    if payload is None:
        return []
    values: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"saved_path", "file_path", "output_path", "path"} and isinstance(value, str):
                values.append(value)
            else:
                values.extend(_path_strings(value))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(_path_strings(item))
    return values


def _is_markdown_file_name(value: str | None) -> bool:
    return bool(value and value.lower().endswith((".md", ".markdown")))
