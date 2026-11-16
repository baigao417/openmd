"""Backend framework primitives.

The V0 implementation only wires skeleton providers, but these dataclasses keep
the code aligned with docs/06-backend-framework.md: backends describe their
capabilities, report dependency health, and extract through one normalized
contract owned by Openmd.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from openmd.core.models import (
    ContentItem,
    InputType,
    SourceLevel,
    SourceType,
    TranscriptResult,
)


BackendLayer = str
DiscoveryStatus = Literal["ok", "failed"]
IdentityResolutionStatus = Literal["resolved", "failed"]


@dataclass(frozen=True)
class BackendCapability:
    platforms: tuple[str, ...]
    source_types: tuple[SourceType, ...]
    input_types: tuple[InputType, ...]
    source_levels: tuple[SourceLevel, ...]
    supports_batch: bool = False
    supports_collection: bool = False
    requires_network: bool = True
    requires_credentials: bool = False
    optional_config: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    layer: BackendLayer
    capabilities: tuple[BackendCapability, ...]
    description: str = ""
    provider: str | None = None
    is_fallback: bool = False


@dataclass(frozen=True)
class BackendHealth:
    ok: bool
    dependencies: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_summary: str | None = None


@dataclass(frozen=True)
class BackendExtractRequest:
    item: ContentItem
    backend_chain: tuple[str, ...]
    allow_fallback: bool = True

    @property
    def primary_backend(self) -> str:
        return self.backend_chain[0] if self.backend_chain else "mock"


@dataclass(frozen=True)
class ItemIdentityResolution:
    item: ContentItem
    status: IdentityResolutionStatus
    provider: str
    error_code: str | None = None
    error_summary: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "resolved" and self.error_code is None

    @classmethod
    def success(cls, item: ContentItem, *, provider: str) -> "ItemIdentityResolution":
        return cls(item=item, status="resolved", provider=provider)

    @classmethod
    def failure(
        cls,
        item: ContentItem,
        *,
        provider: str,
        error_code: str,
        error_summary: str,
    ) -> "ItemIdentityResolution":
        return cls(
            item=item,
            status="failed",
            provider=provider,
            error_code=error_code,
            error_summary=error_summary,
        )


@dataclass(frozen=True)
class DiscoveryRequest:
    """Request to resolve a collection/profile/playlist into extractable items."""

    input_url: str
    platform: str
    limit: int | None = None
    allow_all: bool = False
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryResult:
    """Structured collection discovery result.

    Discovery failures are expected platform/backend outcomes and should be
    returned to the CLI instead of raised as command-level exceptions.
    """

    provider: str
    status: DiscoveryStatus
    items: list[ContentItem] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error_code is None

    @classmethod
    def success(
        cls,
        *,
        provider: str,
        items: list[ContentItem],
        source_notes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "DiscoveryResult":
        return cls(
            provider=provider,
            status="ok",
            items=items,
            source_notes=source_notes or [],
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        *,
        provider: str,
        error_code: str,
        error_summary: str,
        source_notes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "DiscoveryResult":
        return cls(
            provider=provider,
            status="failed",
            error_code=error_code,
            error_summary=error_summary,
            source_notes=source_notes or [],
            metadata=metadata or {},
        )


class BackendProvider(Protocol):
    name: str

    def descriptor(self) -> BackendDescriptor:
        ...

    def health(self) -> BackendHealth:
        ...

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        ...


class DiscoveryProvider(Protocol):
    name: str

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        ...
