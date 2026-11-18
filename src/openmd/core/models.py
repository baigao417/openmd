"""Core dataclasses shared by Openmd adapters and renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


InputType = Literal["single", "batch", "collection", "generic"]
BlockType = Literal["turn", "cue", "table"]
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


def normalize_turn_role(role: str) -> str:
    """Normalize standard AI roles without guessing unknown speaker identities."""
    normalized = role.strip().lower()
    return normalized if normalized in {"user", "assistant", "system", "tool"} else role


@dataclass(frozen=True)
class TurnBlock:
    """One verbatim conversation turn."""

    role: str
    text: str
    time: str | None = None
    message_id: str | None = None
    type: BlockType = "turn"

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", normalize_turn_role(self.role))

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "type": self.type,
                "role": self.role,
                "text": self.text,
                "time": self.time,
                "message_id": self.message_id,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class CueBlock:
    """A time range attached to a transcript or media cue."""

    start: float | str
    end: float | str
    text: str
    type: BlockType = "cue"

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True)
class TableBlock:
    """A table with an explicit marker for merged-cell degradation."""

    rows: list[list[Any]]
    merged_cells: list[dict[str, Any]] = field(default_factory=list)
    type: BlockType = "table"

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"type": self.type, "rows": self.rows}
        if self.merged_cells:
            value["merged_cells"] = self.merged_cells
        return value


def block_as_dict(block: Any) -> dict[str, Any]:
    """Project a typed or legacy mapping block into the package shape."""
    if hasattr(block, "as_dict") and callable(block.as_dict):
        value = block.as_dict()
    elif isinstance(block, dict):
        value = dict(block)
    else:
        raise TypeError("block must be a mapping or typed block")
    kind = value.get("type")
    if kind not in {"turn", "cue", "table"}:
        raise ValueError("unsupported_block_type")
    if kind == "turn" and isinstance(value.get("role"), str):
        value["role"] = normalize_turn_role(value["role"])
    return value


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
    blocks: list[Any] = field(default_factory=list)
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
