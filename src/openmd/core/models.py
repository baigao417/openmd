"""Core dataclasses shared by Openmd adapters and renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


InputType = Literal["single", "batch", "collection", "generic"]
SourceType = Literal[
    "article",
    "video",
    "audio",
    "image_text",
    "mixed",
    "document",
    "conversation",
    "process",
]
Status = Literal["verified", "partial", "failed"]
ComponentStatus = Literal[
    "verified",
    "partial",
    "failed",
    "retryable",
    "unavailable",
    "not_applicable",
    "auth_challenge",
]
FailureClass = Literal[
    "user_resolvable",
    "code_resolvable",
    "source_unavailable",
    "runtime_transient",
]
SourceLevel = Literal[
    "native_text",
    "native_caption",
    "auto_caption",
    "platform_transcript",
    "third_party_extractor",
    "browser_rendered",
    "manual_export",
    "ocr",
    "asr",
    "media_asr",
    "live_capture_asr",
    "partial_extraction",
]

STATUS_VALUES: tuple[str, ...] = ("verified", "partial", "failed")
FAILURE_CLASS_VALUES: tuple[str, ...] = (
    "user_resolvable",
    "code_resolvable",
    "source_unavailable",
    "runtime_transient",
)
SOURCE_LEVEL_VALUES: tuple[str, ...] = (
    "native_text",
    "native_caption",
    "auto_caption",
    "platform_transcript",
    "third_party_extractor",
    "browser_rendered",
    "manual_export",
    "ocr",
    "asr",
    "media_asr",
    "live_capture_asr",
    "partial_extraction",
)
COMPONENT_STATUS_VALUES: tuple[str, ...] = (
    "verified",
    "partial",
    "failed",
    "retryable",
    "unavailable",
    "not_applicable",
    "auth_challenge",
)


@dataclass(frozen=True)
class ExtractionComponent:
    """One independently verifiable part of a composite extraction."""

    id: str
    kind: str
    status: ComponentStatus
    source_level: SourceLevel
    backend: str
    text_ref: str = "index.md#content"
    warnings: list[str] = field(default_factory=list)
    terminal_reason: str | None = None
    error_code: str | None = None
    failure_class: FailureClass | None = None


@dataclass(frozen=True)
class ContentItem:
    platform: str
    input_type: InputType
    url: str
    id: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    source_type: SourceType = "article"
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptResult:
    """V0 normalized extraction result.

    The historical name is kept for CLI stability, but in the backend framework
    this represents extracted text for video, article, audio, and document
    sources. Future versions can promote the alias below to a dedicated
    ExtractionResult model without changing the renderer contract.
    """

    text: str
    status: Status
    source_level: SourceLevel
    backend: str
    source_notes: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_summary: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extraction_contract_version: str | None = None
    components: list[ExtractionComponent] = field(default_factory=list)

    @property
    def has_usable_text(self) -> bool:
        return self.status in {"verified", "partial"} and bool(self.text.strip())

    @property
    def normalized_components(self) -> list[ExtractionComponent]:
        if self.components:
            return list(self.components)
        return [
            ExtractionComponent(
                id="content",
                kind="text",
                status=self.status,
                source_level=self.source_level,
                backend=self.backend,
                warnings=list(self.source_notes),
            )
        ]

    @property
    def has_terminal_gaps(self) -> bool:
        return any(component.status == "unavailable" for component in self.normalized_components)


ExtractionResult = TranscriptResult


@dataclass(frozen=True)
class MarkdownDocument:
    frontmatter: dict[str, Any]
    title: str
    content: str
    source_notes: list[str] = field(default_factory=list)
