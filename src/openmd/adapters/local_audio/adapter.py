"""Adapter for user-local audio files.

This lane is deliberately local and read-only: it resolves audio files into
``ContentItem`` records and leaves transcription to an explicit backend
provider.  Raw audio is referenced as a local source and is not copied into the
package writer's ``assets/`` directory.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse
import hashlib

from openmd.adapters.base import PlatformAdapter
from openmd.core.detector import MEDIA_EXTENSIONS
from openmd.core.models import ContentItem, TranscriptResult

from .asr import MissingASRBackend, MockASRBackend

FASTER_WHISPER_BACKEND = "asr.faster_whisper"
QWEN3_BACKEND = "asr.qwen3"
AUTO_BACKEND = "asr.auto"

AUDIO_EXTENSIONS: tuple[str, ...] = (
    ".mp3",
    ".mp4",
    ".m4a",
    ".wav",
    ".ogg",
    ".flac",
    ".opus",
    ".aac",
)


class LocalAudioAdapter(PlatformAdapter):
    """Resolve local audio files or folders into media items."""

    def __init__(self, platform_id: str = "generic_media", default_backend: str = "asr.fallback") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        path = local_path_from_input(input_value)
        return path is not None and _is_audio_path(path)

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        path = local_path_from_input(input_value)
        if path is None:
            return [
                _content_item_for_unresolved_input(
                    input_value,
                    platform=self.id,
                    error_code="local_audio_path_unresolved",
                )
            ]

        if path.is_dir():
            files = sorted(candidate for candidate in path.rglob("*") if _is_audio_path(candidate))
            if limit is not None and not all:
                files = files[: max(0, limit)]
            return [_content_item_for_path(candidate, platform=self.id) for candidate in files]

        return [_content_item_for_path(path, platform=self.id)]

    def extract_text(
        self,
        item: ContentItem,
        *,
        backend: str | None = None,
        backend_chain: tuple[str, ...] | None = None,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        selected = (backend_chain or (backend or self.default_backend,))[0]
        path = _item_path(item)
        if selected == "local_audio.mock":
            return MockASRBackend().transcribe(path)
        if selected in {FASTER_WHISPER_BACKEND, AUTO_BACKEND, QWEN3_BACKEND}:
            from openmd.runtime.asr import get_asr_backend

            return get_asr_backend(selected).transcribe(path)
        return MissingASRBackend(selected).transcribe(path)


def local_path_from_input(input_value: str) -> Path | None:
    value = input_value.strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme == "file":
        # urlparse("file:///C:/tmp/a.wav").path -> "/C:/tmp/a.wav".
        raw_path = unquote(parsed.path)
        if parsed.netloc and not raw_path:
            raw_path = parsed.netloc
        if raw_path.startswith("/") and len(raw_path) >= 4 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)

    if parsed.scheme in {"http", "https"}:
        return None

    return Path(value)


def _content_item_for_path(path: Path, *, platform: str) -> ContentItem:
    source_ref = path.resolve().as_uri() if path.exists() else str(path)
    return ContentItem(
        platform=platform,
        input_type="single",
        url=source_ref,
        id=_audio_content_id(path),
        title=path.stem,
        source_type="audio",
        raw_meta={
            "profile": "media",
            "permission_scope": "local_file",
            "local_path": str(path),
            "content_id": _audio_content_id(path),
        },
    )


def _content_item_for_unresolved_input(
    input_value: str,
    *,
    platform: str,
    error_code: str,
) -> ContentItem:
    return ContentItem(
        platform=platform,
        input_type="single",
        url=input_value,
        source_type="audio",
        raw_meta={
            "profile": "media",
            "permission_scope": "local_file",
            "error_code": error_code,
        },
    )


def _is_audio_path(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS or path.suffix.lower() in MEDIA_EXTENSIONS


def _audio_content_id(path: Path) -> str:
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:12]
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]


def _item_path(item: ContentItem) -> Path:
    local_path = item.raw_meta.get("local_path") if isinstance(item.raw_meta, dict) else None
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path)
    path = local_path_from_input(item.url)
    return path or Path(item.url)
