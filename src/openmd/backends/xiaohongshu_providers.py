"""Composite Xiaohongshu native text, ordered image OCR, and video transcript provider."""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from openmd.core.opencli_output import load_opencli_json
from openmd.adapters.screenshot.backends.rapidocr import RapidOCRBackend
from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
    ItemIdentityResolution,
)
from openmd.backends.douyin_providers import AsrClient, SiliconFlowASRClient
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.xiaohongshu import (
    classify_xiaohongshu_failure,
    has_required_signed_query,
    normalize_xiaohongshu_url,
    xiaohongshu_user_id,
)
from openmd.backends.youtube_discovery import (
    _is_windows_batch_file,
    _resolve_executable,
    _windows_batch_command_line,
)
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult
from openmd.core.package_contract import XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION
from openmd.core.secrets import sanitize_error_summary, sanitize_url
from openmd.runtime.tempdirs import owned_temporary_directory
from openmd.runtime.ffmpeg_tools import ffmpeg_executable
from openmd.runtime.interpreters import run_interruptibly


XIAOHONGSHU_COMPOSITE_BACKEND = "xiaohongshu.composite"
XIAOHONGSHU_NATIVE_BACKEND = "opencli.openmd-xiaohongshu.note-assets"
XIAOHONGSHU_IMAGE_OCR_BACKEND = "ocr.rapidocr.xiaohongshu"
XIAOHONGSHU_HARD_SUBTITLE_BACKEND = "ocr.rapidocr.xiaohongshu-video"

AssetRunner = Callable[[list[str], int, str | None], tuple[int, str, str]]
CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
ImageOcrExtractor = Callable[[Path, int], str]
HardSubtitleExtractor = Callable[[Path], str]

_TEXT_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_TIMESTAMP_PREFIX_RE = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")


@dataclass(frozen=True)
class XiaohongshuAssetBundle:
    note_id: str
    title: str
    author: str
    native_text: str
    tags: list[str]
    media_kind: str
    native_transcript: str
    image_count: int
    video_count: int
    image_paths: list[Path | None]
    video_paths: list[Path | None]
    image_errors: list[str | None]
    video_errors: list[str | None]


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    requested = options.backend_override
    if options.enable_browser_backends and requested in {None, XIAOHONGSHU_COMPOSITE_BACKEND}:
        registry.register(XIAOHONGSHU_COMPOSITE_BACKEND, XiaohongshuCompositeProvider(options))


def build_opencli_xiaohongshu_note_assets_args(
    input_value: str,
    *,
    output_dir: Path,
    profile_id: str | None,
    download: bool,
    selector_stdin: bool = False,
    command_timeout_seconds: int = 900,
) -> list[str]:
    safe_target = sanitize_url(normalize_xiaohongshu_url(input_value))
    target = "stdin-selector" if selector_stdin and _is_xhslink_selector(safe_target) else safe_target
    command = [
        "opencli",
        "openmd-xiaohongshu",
        "note-assets",
        target,
    ]
    if selector_stdin:
        command.extend(["--selector_stdin", "true"])
    if profile_id:
        command.extend(["--creator", profile_id])
    command.extend(
        [
            "--output",
            str(output_dir),
            "--download",
            "true" if download else "false",
            "--timeout",
            str(max(1, int(command_timeout_seconds))),
            "-f",
            "json",
        ]
    )
    return command


def _is_xhslink_selector(value: str) -> bool:
    try:
        host = urlparse(str(value or "")).netloc.lower()
    except ValueError:
        return False
    return host == "xhslink.com" or host.endswith(".xhslink.com")


class XiaohongshuCompositeProvider:
    """Extract one note into three explicit aggregate components."""

    name = XIAOHONGSHU_COMPOSITE_BACKEND

    def __init__(
        self,
        options: BackendRuntimeOptions,
        *,
        runner: AssetRunner | None = None,
        command_runner: CommandRunner | None = None,
        image_ocr: ImageOcrExtractor | None = None,
        asr_client: AsrClient | None = None,
        hard_subtitle_ocr: HardSubtitleExtractor | None = None,
        opencli_timeout: int = 60,
        ffmpeg_timeout: int = 600,
        asr_timeout: int = 300,
    ) -> None:
        self._options = options
        self._runner = runner or _run_asset_command
        self._command_runner = command_runner or _run_command
        self._image_ocr_override = image_ocr
        self._asr_client = asr_client or SiliconFlowASRClient()
        self._hard_subtitle_override = hard_subtitle_ocr
        self._opencli_timeout = opencli_timeout
        self._ffmpeg_timeout = ffmpeg_timeout
        self._asr_timeout = asr_timeout

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="composite browser/local extraction",
            capabilities=(
                BackendCapability(
                    platforms=("xiaohongshu",),
                    source_types=("mixed", "image_text", "video"),
                    input_types=("single", "collection"),
                    source_levels=("native_text", "platform_transcript", "media_asr", "ocr", "partial_extraction"),
                    requires_network=True,
                    requires_credentials=True,
                    optional_config=(
                        "enable_browser_backends",
                        "enable_media_download_backends",
                        "enable_asr_backends",
                        "enable_cloud_backends",
                        "SILICONFLOW_API_KEY",
                    ),
                ),
            ),
            description="Resolve native note text and temporary media through the project plugin, then local OCR or explicitly authorized cloud ASR.",
            provider="openmd.backends.xiaohongshu_providers",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(
            ok=True,
            dependencies={
                "opencli": "injected" if self._runner is not _run_asset_command else "available" if shutil.which("opencli") or shutil.which("opencli.cmd") else "missing",
                "ffmpeg": "available" if ffmpeg_executable() else "missing",
                self._asr_client.name: "available" if self._asr_client.is_available() else "missing",
            },
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "xiaohongshu":
            return _command_failure(item, "unsupported_platform", "Xiaohongshu composite provider only supports xiaohongshu.")
        runtime_url = str(item.raw_meta.get("runtime_note_url") or "").strip()
        selector = _runtime_selector_for_item(item)
        if not selector and not has_required_signed_query(runtime_url or item.url):
            return _command_failure(
                item,
                "xiaohongshu_signed_url_required",
                "请在浏览器里打开这条笔记再点扩展，或从分享按钮复制完整链接。",
            )
        profile_id = _profile_id_for_item(item)
        download = self._options.enable_media_download_backends
        with owned_temporary_directory(prefix="openmd-xiaohongshu-assets-") as temp_dir:
            temp_root = Path(temp_dir)
            command = build_opencli_xiaohongshu_note_assets_args(
                item.url,
                output_dir=temp_root,
                profile_id=profile_id,
                download=download,
                selector_stdin=bool(selector),
                command_timeout_seconds=self._opencli_timeout,
            )
            exit_code, stdout, stderr = self._runner(command, self._opencli_timeout, selector)
            if exit_code != 0:
                if exit_code == 124:
                    try:
                        from openmd.runtime.services import finish_openmd_bridge
                        finish_openmd_bridge()
                    except (ImportError, OSError, RuntimeError):
                        pass
                    summary = f"OpenCLI note-assets timed out after {self._opencli_timeout} seconds. 请在浏览器中打开笔记后点 Openmd 扩展。"
                else:
                    summary = stderr or stdout or f"OpenCLI exited with code {exit_code}."
                return _command_failure(
                    item,
                    classify_xiaohongshu_failure(stderr or stdout),
                    summary,
                )
            try:
                bundle = parse_opencli_xiaohongshu_note_assets(stdout, temp_root=temp_root)
            except (json.JSONDecodeError, ValueError) as exc:
                return _command_failure(
                    item,
                    "opencli_xiaohongshu_note_assets_invalid_output",
                    exc,
                )
            return self._build_result(item, bundle, download=download)

    def resolve_item_identity(self, item: ContentItem) -> ItemIdentityResolution:
        selector = _runtime_selector_for_item(item)
        if not selector:
            return ItemIdentityResolution.failure(
                item,
                provider=self.name,
                error_code="xiaohongshu_identity_selector_missing",
                error_summary="Xiaohongshu durable identity resolution requires a runtime selector.",
            )
        with owned_temporary_directory(prefix="openmd-xiaohongshu-identity-") as temp_dir:
            command = build_opencli_xiaohongshu_note_assets_args(
                item.url,
                output_dir=Path(temp_dir),
                profile_id=_profile_id_for_item(item),
                download=False,
                selector_stdin=True,
                command_timeout_seconds=self._opencli_timeout,
            )
            exit_code, stdout, stderr = self._runner(
                command,
                self._opencli_timeout,
                selector,
            )
            if exit_code != 0:
                return ItemIdentityResolution.failure(
                    item,
                    provider=self.name,
                    error_code=classify_xiaohongshu_failure(stderr or stdout),
                    error_summary=sanitize_error_summary(stderr or stdout)
                    or "Xiaohongshu identity resolution failed.",
                )
            try:
                bundle = parse_opencli_xiaohongshu_note_assets(
                    stdout,
                    temp_root=Path(temp_dir),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                return ItemIdentityResolution.failure(
                    item,
                    provider=self.name,
                    error_code="xiaohongshu_identity_invalid_output",
                    error_summary=sanitize_error_summary(exc)
                    or "Xiaohongshu identity output was invalid.",
                )

        note_id = bundle.note_id.strip().lower()
        if not note_id:
            return ItemIdentityResolution.failure(
                item,
                provider=self.name,
                error_code="xiaohongshu_identity_missing_note_id",
                error_summary="Xiaohongshu identity output did not contain a note ID.",
            )
        media_kind = _normalized_media_kind(bundle.media_kind)
        raw_meta = dict(item.raw_meta)
        raw_meta.update(
            {
                "content_id": note_id,
                "media_kind": media_kind,
                "profile": "media" if media_kind in {"video", "mixed"} else "document",
                "extraction_contract_version": XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
                "required_components": _required_components_for_media_kind(media_kind),
                "identity_unresolved": False,
            }
        )
        resolved = replace(
            item,
            id=note_id,
            url=f"https://www.xiaohongshu.com/discovery/item/{note_id}",
            title=bundle.title or item.title,
            author=bundle.author or item.author,
            source_type=_source_type_for_media_kind(media_kind),
            raw_meta=raw_meta,
        )
        return ItemIdentityResolution.success(resolved, provider=self.name)

    def _build_result(
        self,
        item: ContentItem,
        bundle: XiaohongshuAssetBundle,
        *,
        download: bool,
    ) -> TranscriptResult:
        native_section, native_component = _native_component(bundle)
        image_section, image_component = self._image_component(bundle, download=download)
        video_section, video_component = self._video_component(bundle, download=download)
        components = [native_component, image_component, video_component]
        text = (
            "## Native Note Text\n\n"
            f"{native_section}\n\n"
            "## Image OCR\n\n"
            f"{image_section}\n\n"
            "## Video Transcript\n\n"
            f"{video_section}\n"
        )
        usable = any(component.status == "verified" for component in components)
        blocking = any(component.status in {"failed", "retryable", "partial", "auth_challenge"} for component in components)
        terminal_gap = any(component.status == "unavailable" for component in components)
        status = "failed" if not usable else "partial" if blocking or terminal_gap else "verified"
        source_level = _primary_source_level(components)
        return TranscriptResult(
            text=text,
            status=status,
            source_level=source_level,
            backend=self.name,
            title=bundle.title or item.title,
            author=bundle.author or item.author,
            published_at=item.published_at,
            source_notes=[
                "Project-owned OpenCLI resolved note metadata and downloaded only into a caller-owned temporary directory.",
                "Ordered image OCR, native transcript, ASR, and hard-subtitle OCR provenance remain separate components.",
                "Signed note URLs, CDN URLs, cookies, headers, raw browser stores, and temporary media were not persisted.",
            ],
            metadata={
                "profile": "media" if bundle.media_kind in {"video", "mixed"} else "document",
                "content_id": bundle.note_id or item.id,
                "media_kind": bundle.media_kind,
                "image_count": bundle.image_count,
                "video_count": bundle.video_count,
                "media_retention": "none",
                "temp_cleanup": True,
            },
            extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
            components=components,
        )

    def _image_component(
        self,
        bundle: XiaohongshuAssetBundle,
        *,
        download: bool,
    ) -> tuple[str, ExtractionComponent]:
        if bundle.media_kind not in {"image_text", "mixed"} or bundle.image_count <= 0:
            return "_Not applicable._", _not_applicable("image_ocr", "image_ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND, "index.md#image-ocr")
        if not download:
            return "_Unavailable until media download is enabled._", _retryable(
                "image_ocr", "image_ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND,
                "xiaohongshu_image_download_not_enabled", "Enable media download to OCR note images.", "index.md#image-ocr",
            )
        sections: list[str] = []
        success = 0
        retryable_codes: list[str] = []
        for index in range(1, bundle.image_count + 1):
            path = bundle.image_paths[index - 1]
            text = ""
            if path is None:
                retryable_codes.append(bundle.image_errors[index - 1] or "xiaohongshu_image_asset_missing")
            else:
                try:
                    text = self._image_ocr(path, index)
                except Exception as exc:
                    retryable_codes.append(_retryable_code(exc, prefix="xiaohongshu_image_ocr"))
                    text = ""
            sections.extend([f"### Image {index}", "", text.strip() or "_OCR unavailable or empty._"])
            if _has_meaningful_text(text):
                success += 1
        if retryable_codes:
            component = _retryable(
                "image_ocr", "image_ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND,
                retryable_codes[0],
                f"Image OCR requires retry for {len(retryable_codes)} of {bundle.image_count} ordered slots.",
                "index.md#image-ocr",
                failure_class="runtime_transient",
            )
        elif success == bundle.image_count:
            component = ExtractionComponent(
                "image_ocr", "image_ocr", "verified", "ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND,
                text_ref="index.md#image-ocr",
            )
        elif success > 0:
            component = ExtractionComponent(
                "image_ocr", "image_ocr", "partial", "ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND,
                text_ref="index.md#image-ocr",
                warnings=[f"OCR produced text for {success} of {bundle.image_count} images."],
                error_code="xiaohongshu_image_ocr_partial",
                failure_class="runtime_transient",
            )
        else:
            component = _unavailable(
                "image_ocr", "image_ocr", XIAOHONGSHU_IMAGE_OCR_BACKEND,
                "xiaohongshu_image_ocr_empty", "No meaningful OCR text was available from the ordered images.", "index.md#image-ocr",
            )
        return "\n\n".join(sections), component

    def _video_component(
        self,
        bundle: XiaohongshuAssetBundle,
        *,
        download: bool,
    ) -> tuple[str, ExtractionComponent]:
        if bundle.media_kind not in {"video", "mixed"} or bundle.video_count <= 0:
            return "_Not applicable._", _not_applicable("video_transcript", "video_transcript", XIAOHONGSHU_NATIVE_BACKEND, "index.md#video-transcript")
        native = _format_transcript(bundle.native_transcript)
        if _has_meaningful_text(native):
            return native, ExtractionComponent(
                "video_transcript", "video_transcript", "verified", "platform_transcript", XIAOHONGSHU_NATIVE_BACKEND,
                text_ref="index.md#video-transcript",
            )
        if not download:
            return "_Unavailable until media download is enabled._", _retryable(
                "video_transcript", "video_transcript", self._asr_client.name,
                "xiaohongshu_video_download_not_enabled", "Enable media download for video transcription.", "index.md#video-transcript",
            )
        video_path = next((path for path in bundle.video_paths if path is not None), None)
        if video_path is None:
            code = next((code for code in bundle.video_errors if code), "xiaohongshu_video_asset_missing")
            return "_Video asset download requires retry._", _retryable(
                "video_transcript", "video_transcript", self._asr_client.name,
                code, "The note exposed video metadata but no temporary video was downloaded.", "index.md#video-transcript",
                failure_class="runtime_transient",
            )
        if not self._options.enable_asr_backends:
            return "_Unavailable until ASR is enabled._", _retryable(
                "video_transcript", "video_transcript", self._asr_client.name,
                "xiaohongshu_asr_not_enabled", "Enable ASR to transcribe videos without native subtitles.", "index.md#video-transcript",
            )
        if not self._options.enable_cloud_backends:
            return "_Unavailable until cloud ASR is authorized._", _retryable(
                "video_transcript", "video_transcript", self._asr_client.name,
                "xiaohongshu_cloud_backends_not_enabled",
                "Enable --enable-cloud-backends before preparing audio for cloud ASR.",
                "index.md#video-transcript",
            )
        if not self._asr_client.is_available():
            return "_ASR backend unavailable._", _retryable(
                "video_transcript", "video_transcript", self._asr_client.name,
                "xiaohongshu_asr_backend_unavailable", "The configured ASR backend is unavailable.", "index.md#video-transcript",
            )
        asr_error = ""
        retryable_code = ""
        explicit_no_speech = False
        try:
            audio_path = video_path.with_suffix(".mp3")
            self._extract_audio(video_path, audio_path)
            asr_text = _format_transcript(self._asr_client.transcribe(audio_path, timeout=self._asr_timeout))
            if _has_meaningful_text(asr_text):
                return asr_text, ExtractionComponent(
                    "video_transcript", "video_transcript", "verified", "media_asr", self._asr_client.name,
                    text_ref="index.md#video-transcript",
                )
            asr_error = "ASR returned no meaningful speech."
            explicit_no_speech = True
        except Exception as exc:  # noqa: BLE001 - hard-subtitle OCR is the approved fallback.
            asr_error = sanitize_error_summary(exc)
            if _is_explicit_no_speech(asr_error):
                explicit_no_speech = True
            else:
                retryable_code = _retryable_code(exc, prefix="xiaohongshu_video_asr")
        try:
            hard_text = self._hard_subtitle_ocr(video_path)
        except Exception as exc:  # noqa: BLE001 - return structured terminal gap.
            hard_text = ""
            asr_error = f"{asr_error}; hard subtitle OCR failed: {sanitize_error_summary(exc)}"
            retryable_code = retryable_code or _retryable_code(exc, prefix="xiaohongshu_hard_subtitle_ocr")
        if _has_meaningful_text(hard_text):
            return hard_text.strip() + "\n", ExtractionComponent(
                "video_transcript", "video_transcript", "verified", "ocr", XIAOHONGSHU_HARD_SUBTITLE_BACKEND,
                text_ref="index.md#video-transcript",
                warnings=[f"ASR did not produce usable text: {asr_error}"],
            )
        if retryable_code:
            return "_Video transcript extraction requires retry._", _retryable(
                "video_transcript", "video_transcript", XIAOHONGSHU_HARD_SUBTITLE_BACKEND,
                retryable_code, asr_error or "Video transcript extraction failed transiently.", "index.md#video-transcript",
                failure_class="runtime_transient",
            )
        return "_No usable speech or hard-subtitle text._", _unavailable(
            "video_transcript", "video_transcript", XIAOHONGSHU_HARD_SUBTITLE_BACKEND,
            "xiaohongshu_video_transcript_unavailable", asr_error or "No usable speech or hard-subtitle text was found.", "index.md#video-transcript",
        )

    def _image_ocr(self, path: Path, index: int) -> str:
        if self._image_ocr_override is not None:
            return self._image_ocr_override(path, index)
        result = RapidOCRBackend().recognize(str(path), title=f"Image {index}")
        if result.status == "failed" and result.error_code not in {None, "ocr_empty_text"}:
            raise RuntimeError(result.error_code)
        return _strip_generated_title(result.text)

    def _extract_audio(self, video_path: Path, audio_path: Path) -> None:
        ffmpeg = ffmpeg_executable() or "ffmpeg"
        command = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path),
        ]
        exit_code, _stdout, stderr = self._command_runner(command, self._ffmpeg_timeout)
        if exit_code != 0 or not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {sanitize_error_summary(stderr)}")

    def _hard_subtitle_ocr(self, video_path: Path) -> str:
        if self._hard_subtitle_override is not None:
            return self._hard_subtitle_override(video_path)
        ffmpeg = ffmpeg_executable() or "ffmpeg"
        with owned_temporary_directory(prefix="openmd-xiaohongshu-frames-") as frames_dir:
            pattern = Path(frames_dir) / "frame_%05d.jpg"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-vf", "fps=0.5,crop=iw:ih*0.52:0:ih*0.45", "-q:v", "3", str(pattern),
            ]
            exit_code, _stdout, stderr = self._command_runner(command, self._ffmpeg_timeout)
            if exit_code != 0:
                raise RuntimeError(f"ffmpeg frame extraction failed: {sanitize_error_summary(stderr)}")
            segments: list[str] = []
            previous = ""
            for index, frame in enumerate(sorted(Path(frames_dir).glob("frame_*.jpg")), start=1):
                text = self._image_ocr(frame, index).strip()
                normalized = re.sub(r"\s+", "", text)
                if not normalized:
                    continue
                similarity = difflib.SequenceMatcher(None, normalized, previous).ratio() if previous else 0.0
                if similarity < 0.82:
                    segments.append(text)
                    previous = normalized
            return "\n".join(segments)


def parse_opencli_xiaohongshu_note_assets(stdout: str, *, temp_root: Path) -> XiaohongshuAssetBundle:
    payload = load_opencli_json(str(stdout or "").strip())
    rows = payload if isinstance(payload, list) else payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Xiaohongshu note-assets output must be JSON rows.")
    note = next((row for row in rows if isinstance(row, dict) and row.get("row_type") == "note"), None)
    if not isinstance(note, dict):
        raise ValueError("Xiaohongshu note-assets output had no note metadata row.")
    image_count = max(0, int(note.get("image_count") or 0))
    video_count = max(0, int(note.get("video_count") or 0))
    image_paths: list[Path | None] = [None] * image_count
    video_paths: list[Path | None] = [None] * video_count
    image_errors: list[str | None] = ["xiaohongshu_image_asset_missing"] * image_count
    video_errors: list[str | None] = ["xiaohongshu_video_asset_missing"] * video_count
    asset_rows = sorted(
        (row for row in rows if isinstance(row, dict) and row.get("row_type") == "asset"),
        key=lambda row: int(row.get("asset_index") or 0),
    )
    for row in asset_rows:
        if not isinstance(row, dict) or row.get("row_type") != "asset":
            continue
        asset_type = str(row.get("asset_type") or "")
        asset_index = max(0, int(row.get("asset_index") or 0))
        slot = asset_index - 1 if asset_type == "image" else asset_index - image_count - 1
        slots = image_paths if asset_type == "image" else video_paths
        errors = image_errors if asset_type == "image" else video_errors
        if slot < 0 or slot >= len(slots):
            continue
        if row.get("asset_status") != "success":
            errors[slot] = str(row.get("asset_error_code") or f"xiaohongshu_{asset_type}_download_failed")
            continue
        local_path = _safe_local_asset_path(temp_root, str(row.get("local_path") or ""))
        if local_path is None or not local_path.is_file():
            errors[slot] = f"xiaohongshu_{asset_type}_asset_missing"
            continue
        slots[slot] = local_path
        errors[slot] = None
    return XiaohongshuAssetBundle(
        note_id=str(note.get("note_id") or ""),
        title=str(note.get("title") or ""),
        author=str(note.get("author") or ""),
        native_text=str(note.get("native_text") or ""),
        tags=[str(value) for value in note.get("tags", []) if isinstance(value, str)] if isinstance(note.get("tags"), list) else [],
        media_kind=str(note.get("media_kind") or "text"),
        native_transcript=str(note.get("native_transcript") or ""),
        image_count=image_count,
        video_count=video_count,
        image_paths=image_paths,
        video_paths=video_paths,
        image_errors=image_errors,
        video_errors=video_errors,
    )


def _profile_id_for_item(item: ContentItem) -> str | None:
    for value in (
        item.raw_meta.get("profile_id"),
        item.raw_meta.get("collection_url"),
        item.raw_meta.get("profile_url"),
    ):
        profile_id = xiaohongshu_user_id(str(value or ""))
        if profile_id:
            return profile_id
    return None


def _runtime_selector_for_item(item: ContentItem) -> str | None:
    for key in ("runtime_note_url", "runtime_selector", "runtime_profile_selector"):
        value = str(item.raw_meta.get(key) or "").strip()
        if value:
            return value
    parsed = normalize_xiaohongshu_url(item.url)
    host = urlparse(parsed).netloc.lower()
    if host == "xhslink.com" or host.endswith(".xhslink.com"):
        return parsed
    return None


def _normalized_media_kind(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"text", "image_text", "video", "mixed"}:
        return normalized
    return "mixed"


def _required_components_for_media_kind(media_kind: str) -> list[str]:
    if media_kind == "text":
        return ["native_text"]
    if media_kind == "image_text":
        return ["native_text", "image_ocr"]
    if media_kind == "video":
        return ["native_text", "video_transcript"]
    return ["native_text", "image_ocr", "video_transcript"]


def _source_type_for_media_kind(media_kind: str) -> str:
    if media_kind == "text":
        return "article"
    if media_kind in {"image_text", "video"}:
        return media_kind
    return "mixed"


def _native_component(bundle: XiaohongshuAssetBundle) -> tuple[str, ExtractionComponent]:
    parts = [bundle.native_text.strip()]
    if bundle.tags:
        parts.append("Tags: " + ", ".join(bundle.tags))
    text = "\n\n".join(part for part in parts if part).strip()
    if not text and bundle.title:
        text = "_The creator title is preserved in the document heading; no separate body text was present._"
    if _has_meaningful_text(text) or bundle.title:
        return text, ExtractionComponent(
            "native_text", "native_text", "verified", "native_text", XIAOHONGSHU_NATIVE_BACKEND,
            text_ref="index.md#native-note-text",
        )
    return "_Native note text unavailable._", _unavailable(
        "native_text", "native_text", XIAOHONGSHU_NATIVE_BACKEND,
        "xiaohongshu_native_text_unavailable", "No creator-authored title, body, or tags were available.", "index.md#native-note-text",
    )


def _not_applicable(component_id: str, kind: str, backend: str, text_ref: str) -> ExtractionComponent:
    return ExtractionComponent(component_id, kind, "not_applicable", "partial_extraction", backend, text_ref=text_ref)


def _retryable(
    component_id: str, kind: str, backend: str, code: str, warning: str, text_ref: str,
    *, failure_class: str = "user_resolvable",
) -> ExtractionComponent:
    return ExtractionComponent(
        component_id, kind, "retryable", "partial_extraction", backend,
        text_ref=text_ref, warnings=[warning], error_code=code, failure_class=failure_class,
    )


def _unavailable(
    component_id: str, kind: str, backend: str, code: str, reason: str, text_ref: str,
) -> ExtractionComponent:
    return ExtractionComponent(
        component_id, kind, "unavailable", "partial_extraction", backend,
        text_ref=text_ref, warnings=[reason], terminal_reason=reason,
        error_code=code, failure_class="source_unavailable",
    )


def _primary_source_level(components: list[ExtractionComponent]) -> str:
    for component in components:
        if component.status == "verified" and component.id == "native_text":
            return "native_text"
    for component in components:
        if component.status == "verified":
            return component.source_level
    return "partial_extraction"


def _safe_local_asset_path(root: Path, value: str) -> Path | None:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        return None
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _format_transcript(text: str) -> str:
    lines: list[str] = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = _TIMESTAMP_PREFIX_RE.sub("", raw).strip()
        if _has_meaningful_text(line):
            lines.append(f"[00:00:00] {line}")
    if not lines and _has_meaningful_text(text):
        lines = [f"[00:00:00] {str(text).strip()}"]
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _has_meaningful_text(value: str) -> bool:
    return len(_TEXT_TOKEN_RE.findall(str(value or ""))) >= 2


def _strip_generated_title(text: str) -> str:
    lines = str(text or "").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _is_explicit_no_speech(value: str) -> bool:
    text = str(value or "").lower()
    return any(term in text for term in ("no meaningful speech", "no meaningful text", "no speech"))


def _retryable_code(error: object, *, prefix: str) -> str:
    text = str(error or "").lower()
    if "not available" in text or "not found" in text or "missing" in text:
        return f"{prefix}_dependency_missing"
    if "timeout" in text or "timed out" in text:
        return f"{prefix}_timeout"
    if "http" in text or any(code in text for code in ("403", "404", "429", "500", "502", "503")):
        return f"{prefix}_http_error"
    return f"{prefix}_retryable"


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    completed = run_interruptibly(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _run_asset_command(
    command: list[str],
    timeout: int,
    selector: str | None,
) -> tuple[int, str, str]:
    env = None
    if Path(command[0]).name.lower() in {"opencli", "opencli.cmd", "opencli.exe"}:
        from openmd.runtime.managed_tools import command_environment, managed_command
        resolved = managed_command(command[1:])
        env = command_environment()
    else:
        resolved = [_resolve_executable(command[0]), *command[1:]]
    popen_args: list[str] | str = resolved
    if _is_windows_batch_file(resolved[0]):
        popen_args = _windows_batch_command_line(resolved)
    try:
        completed = run_interruptibly(
            popen_args,
            input=selector or "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"OpenCLI note-assets timed out after {timeout} seconds."
    except OSError as exc:
        return 127, "", sanitize_error_summary(exc)
    return completed.returncode, completed.stdout, completed.stderr


def _command_failure(
    item: ContentItem,
    code: str,
    summary: object,
) -> TranscriptResult:
    if _is_terminal_source_unavailable_code(code):
        reason = sanitize_error_summary(summary) or "The Xiaohongshu note is unavailable."
        components = [
            _unavailable(
                component_id,
                component_id,
                XIAOHONGSHU_COMPOSITE_BACKEND,
                code,
                reason,
                f"index.md#{component_id.replace('_', '-')}",
            )
            for component_id in ("native_text", "image_ocr", "video_transcript")
        ]
        text = (
            "## Native Note Text\n\n_Source unavailable._\n\n"
            "## Image OCR\n\n_Source unavailable._\n\n"
            "## Video Transcript\n\n_Source unavailable._\n"
        )
        media_kind = str(item.raw_meta.get("media_kind") or "mixed")
        return TranscriptResult(
            text=text,
            status="partial",
            source_level="partial_extraction",
            backend=XIAOHONGSHU_COMPOSITE_BACKEND,
            source_notes=[
                "Xiaohongshu reported a durable terminal source gap; no raw response or transient locator was persisted."
            ],
            error_code=code,
            error_summary=reason,
            title=item.title or item.id or "Unavailable Xiaohongshu note",
            author=item.author,
            published_at=item.published_at,
            metadata={
                "profile": "media" if media_kind in {"video", "mixed"} else "document",
                "media_kind": media_kind,
                "media_retention": "none",
                "temp_cleanup": True,
            },
            extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
            components=components,
        )

    auth = "auth" in code or "session" in code
    status = "auth_challenge" if auth else "failed"
    failure_class = "user_resolvable" if auth else "runtime_transient"
    components = [
        ExtractionComponent(
            component_id,
            component_id,
            status,
            "partial_extraction",
            XIAOHONGSHU_COMPOSITE_BACKEND,
            text_ref=f"index.md#{component_id.replace('_', '-')}",
            warnings=[sanitize_error_summary(summary)],
            error_code=code,
            failure_class=failure_class,
        )
        for component_id in ("native_text", "image_ocr", "video_transcript")
    ]
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=XIAOHONGSHU_COMPOSITE_BACKEND,
        source_notes=["Xiaohongshu composite provider returned a structured command failure."],
        error_code=code,
        error_summary=sanitize_error_summary(summary),
        title=item.title,
        author=item.author,
        published_at=item.published_at,
        metadata={"media_retention": "none", "temp_cleanup": True},
        extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
        components=components,
    )


def _is_terminal_source_unavailable_code(code: str) -> bool:
    normalized = str(code or "").strip().lower()
    return normalized in {
        "xiaohongshu_not_found",
        "xiaohongshu_removed",
        "xiaohongshu_deleted",
        "xiaohongshu_source_unavailable",
    }
