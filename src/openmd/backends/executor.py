"""Backend chain executor for normalized extraction.

The executor owns chain semantics: try the selected backend first, then continue
to fallbacks only when the previous provider returned a structured failure.
Individual providers receive a single-backend request so they do not perform
their own hidden fallback loops.
"""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable

from openmd.adapters.base import PlatformAdapter
from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
    BackendProvider,
    DiscoveryProvider,
    DiscoveryRequest,
    DiscoveryResult,
    ItemIdentityResolution,
)


class BackendProviderRegistry:
    """Provider lookup with an explicit safe skeleton mode.

    The default Openmd CLI uses skeleton mode so backend names in a chain remain
    route declarations until a real provider is intentionally registered.
    """

    def __init__(
        self,
        providers: dict[str, BackendProvider] | None = None,
        *,
        allow_skeleton_fallback: bool = False,
    ) -> None:
        self._providers: dict[str, BackendProvider] = {"mock": MockBackendProvider()}
        self._providers.update(providers or {})
        self.allow_skeleton_fallback = allow_skeleton_fallback

    def register(self, name: str, provider: BackendProvider) -> None:
        self._providers[name] = provider

    def provider_for(self, backend: str) -> BackendProvider | None:
        provider = self._providers.get(backend)
        if provider is not None:
            return provider
        if self.allow_skeleton_fallback:
            return SkeletonBackendProvider(backend)
        return None


class DiscoveryProviderRegistry:
    """Discovery provider lookup with safe non-network defaults."""

    def __init__(
        self,
        providers: dict[str, DiscoveryProvider] | None = None,
        *,
        platform_defaults: dict[str, str] | None = None,
        allow_skeleton_fallback: bool = True,
    ) -> None:
        self._providers: dict[str, DiscoveryProvider] = {
            "skeleton.discovery": SkeletonDiscoveryProvider(),
            "mock.discovery": MockDiscoveryProvider(),
        }
        self._providers.update(providers or {})
        self._platform_defaults: dict[str, str] = dict(platform_defaults or {})
        self.allow_skeleton_fallback = allow_skeleton_fallback

    def register(
        self,
        name: str,
        provider: DiscoveryProvider,
        *,
        platforms: tuple[str, ...] = (),
        default_for_platforms: bool = True,
    ) -> None:
        self._providers[name] = provider
        if default_for_platforms:
            for platform in platforms:
                self._platform_defaults[platform] = name

    def provider_for(
        self,
        request: DiscoveryRequest,
        *,
        requested_provider: str | None = None,
    ) -> DiscoveryProvider | None:
        name = requested_provider or self._platform_defaults.get(request.platform)
        if name:
            provider = self._providers.get(name)
            if provider is not None:
                return provider
        if self.allow_skeleton_fallback:
            return self._providers["skeleton.discovery"]
        return None


class SkeletonBackendProvider:
    """Safe placeholder that never calls network, accounts, commands, ASR, or OCR."""

    def __init__(self, name: str) -> None:
        self.name = name

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="skeleton",
            capabilities=(),
            description="Safe placeholder provider; real extraction is not implemented.",
            provider="openmd.skeleton",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, warnings=("skeleton provider only",))

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        backend = request.primary_backend
        return _failed_result(
            request.item,
            backend,
            "backend_not_implemented",
            "Skeleton provider only; no external command, network, account, ASR, or OCR action was attempted.",
            notes=[
                "skeleton provider only; real extraction is not implemented",
                "no external command/network/account/asr/ocr action was attempted",
            ],
        )


class MockBackendProvider:
    """Deterministic local smoke provider for tests and CLI skeleton checks."""

    name = "mock"

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("*",),
            source_types=("article", "video", "audio", "image_text", "mixed", "document"),
            input_types=("single", "batch", "collection", "generic"),
            source_levels=("third_party_extractor", "platform_transcript"),
            requires_network=False,
        )
        return BackendDescriptor(
            name=self.name,
            layer="mock",
            capabilities=(capability,),
            description="Local deterministic backend for smoke checks.",
            provider="openmd.mock",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={"mock": "ok"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        return TranscriptResult(
            text="[00:00:00] Mock transcript for Openmd skeleton.\n",
            status="verified",
            # The video fixture must satisfy the same source contract as real
            # providers. Its backend/notes still identify this as synthetic.
            source_level="platform_transcript" if request.item.platform == "douyin" else "third_party_extractor",
            backend=request.primary_backend,
            source_notes=["local smoke check only"],
        )


def create_safe_backend_provider_registry() -> BackendProviderRegistry:
    """Create the default provider registry used by CLI skeleton execution."""
    return BackendProviderRegistry(allow_skeleton_fallback=True)


class SkeletonDiscoveryProvider:
    """Safe placeholder that never discovers through network, browser, CLI, ASR, or OCR."""

    name = "skeleton.discovery"

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        return _failed_discovery_result(
            self.name,
            "discovery_not_implemented",
            (
                "Skeleton discovery provider only; no external command, network, "
                "browser, account/session, yt-dlp, ASR, or OCR action was attempted."
            ),
            notes=[
                "skeleton discovery provider only; real collection discovery is not implemented",
                "no external command/network/browser/account/session/yt-dlp/asr/ocr action was attempted",
            ],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "allow_all": request.allow_all,
            },
        )


class MockDiscoveryProvider:
    """Deterministic local discovery provider for tests and CLI smoke checks."""

    name = "mock.discovery"

    def __init__(
        self,
        *,
        items: list[ContentItem] | None = None,
        default_count: int = 2,
    ) -> None:
        self._items = items
        self.default_count = default_count

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if self._items is not None:
            items = list(self._items)
            if request.limit is not None and not request.allow_all:
                items = items[: max(0, request.limit)]
        else:
            count = self.default_count
            if request.limit is not None and not request.allow_all:
                count = min(count, max(0, request.limit))
            items = [
                ContentItem(
                    platform=request.platform,
                    input_type="single",
                    url=f"https://www.youtube.com/watch?v=openmd-mock-{index + 1}",
                    id=f"openmd-mock-{index + 1}",
                    title=f"Openmd mock video {index + 1}",
                    author="Openmd mock",
                    source_type="video",
                    raw_meta={
                        "collection_url": request.input_url,
                        "discovery_limit": request.limit,
                        "discovery_provider": self.name,
                    },
                )
                for index in range(count)
            ]
        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=["mock discovery provider returned deterministic local items"],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "allow_all": request.allow_all,
            },
        )


def create_safe_discovery_provider_registry() -> DiscoveryProviderRegistry:
    """Create the default discovery registry used by CLI collect skeleton execution."""
    return DiscoveryProviderRegistry(allow_skeleton_fallback=True)




class BackendExecutor:
    """Execute a backend chain until one provider returns usable text."""

    def __init__(
        self,
        providers: dict[str, BackendProvider] | None = None,
        *,
        provider_registry: BackendProviderRegistry | None = None,
        default_provider: BackendProvider | None = None,
        result_enricher: Callable[[ContentItem, TranscriptResult], TranscriptResult] | None = None,
    ) -> None:
        self._provider_registry = provider_registry or BackendProviderRegistry(providers)
        if provider_registry is not None:
            for name, provider in (providers or {}).items():
                self._provider_registry.register(name, provider)
        self._default_provider = default_provider
        self._result_enricher = result_enricher

    def extract(
        self,
        item: ContentItem,
        backend_chain: tuple[str, ...],
        *,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        return self.execute(
            BackendExtractRequest(
                item=item,
                backend_chain=backend_chain,
                allow_fallback=allow_fallback,
            )
        )

    def resolve_item_identity(
        self,
        item: ContentItem,
        backend_chain: tuple[str, ...],
    ) -> ItemIdentityResolution:
        """Resolve optional durable identity before skip-existing checks."""
        if not bool(item.raw_meta.get("identity_unresolved")):
            return ItemIdentityResolution.success(item, provider="identity.already_resolved")

        for backend in backend_chain:
            provider = self._provider_for(backend)
            resolver = getattr(provider, "resolve_item_identity", None) if provider else None
            if not callable(resolver):
                continue
            try:
                resolved = resolver(item)
            except Exception as exc:  # pragma: no cover - provider boundary safety net
                return ItemIdentityResolution.failure(
                    item,
                    provider=getattr(provider, "name", backend),
                    error_code="identity_resolution_provider_error",
                    error_summary=sanitize_error_summary(exc) or "identity resolution failed",
                )
            if isinstance(resolved, ItemIdentityResolution):
                return resolved
            if isinstance(resolved, ContentItem):
                return ItemIdentityResolution.success(
                    resolved,
                    provider=getattr(provider, "name", backend),
                )
            return ItemIdentityResolution.failure(
                item,
                provider=getattr(provider, "name", backend),
                error_code="identity_resolution_invalid_result",
                error_summary="Identity resolver returned an unsupported result.",
            )

        return ItemIdentityResolution.failure(
            item,
            provider="identity.not_available",
            error_code="identity_resolver_not_available",
            error_summary="No selected backend can resolve the durable item identity.",
        )

    def execute(self, request: BackendExtractRequest) -> TranscriptResult:
        chain = request.backend_chain if request.allow_fallback else request.backend_chain[:1]
        if not chain:
            return _failed_result(request.item, "not_selected", "backend_chain_empty")

        attempt_notes: list[str] = [f"backend_chain: {' -> '.join(chain)}"]
        attempts: list[dict[str, str]] = []
        last_result: TranscriptResult | None = None
        current_item = request.item

        for backend in chain:
            provider = self._provider_for(backend)
            if provider is None:
                result = _failed_result(request.item, backend, "backend_not_registered")
            else:
                result = self._extract_one(provider, current_item, backend)

            # Engine detection is evidence for every later fallback. Generic
            # article readers cannot replace a known missing forum body with UI.
            if result.metadata.get("engine") == "discourse" or result.metadata.get("site_engine") == "discourse":
                current_item = replace(current_item, raw_meta={**current_item.raw_meta, "site_engine": "discourse"})

            from openmd.routing.loader import route_trace

            result = replace(result, metadata={**result.metadata, **route_trace(request.item.platform, result.backend)})

            if result.has_usable_text or (result.status == "partial" and result.metadata.get("terminal_partial") is True):
                if self._result_enricher is not None:
                    result = self._result_enricher(request.item, result)
                return _with_attempt_notes(result, attempt_notes)

            # A later "tool not installed" must not hide an earlier verdict about
            # the source itself (error page, challenge, no subtitles).
            if last_result is None or not _is_tool_unavailable(result) or _is_tool_unavailable(last_result):
                last_result = result
            attempts.append({"backend": backend, "error_code": result.error_code or result.status})
            attempt_notes.append(
                f"fallback_attempt: {backend} failed with {result.error_code or result.status}"
            )

        if last_result is not None:
            last_result = replace(last_result, metadata={**last_result.metadata, "fallback_attempts": attempts})
        return _with_attempt_notes(last_result, attempt_notes) if last_result else _failed_result(
            request.item,
            chain[-1],
            "backend_chain_empty",
        )

    def _provider_for(self, backend: str) -> BackendProvider | None:
        return self._provider_registry.provider_for(backend) or self._default_provider

    def _extract_one(
        self,
        provider: BackendProvider,
        item: ContentItem,
        backend: str,
    ) -> TranscriptResult:
        try:
            result = provider.extract(
                BackendExtractRequest(
                    item=item,
                    backend_chain=(backend,),
                    allow_fallback=False,
                )
            )
            return validate_extraction_content(result, item)
        except Exception as exc:  # pragma: no cover - final provider safety net
            return _failed_result(item, backend, "backend_provider_error", exc)


def _failed_result(
    item: ContentItem,
    backend: str,
    code: str,
    error: object | None = None,
    *,
    notes: list[str] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=["backend executor returned structured failure", *(notes or [])],
        error_code=code,
        error_summary=sanitize_error_summary(error or code),
        metadata={"platform": item.platform},
    )


_TOOL_UNAVAILABLE_CODES = frozenset({
    "backend_exit_127", "backend_exit_69", "tool_not_installed", "backend_dependency_missing", "backend_not_registered",
    "backend_not_implemented", "dependency_missing", "not_installed",
})


def _is_tool_unavailable(result: TranscriptResult) -> bool:
    return str(result.error_code or "") in _TOOL_UNAVAILABLE_CODES


def _with_attempt_notes(result: TranscriptResult, attempt_notes: list[str]) -> TranscriptResult:
    return replace(result, source_notes=[*result.source_notes, *attempt_notes])


class DiscoveryExecutor:
    """Resolve collection inputs into extractable ContentItem records."""

    def __init__(
        self,
        providers: dict[str, DiscoveryProvider] | None = None,
        *,
        provider_registry: DiscoveryProviderRegistry | None = None,
        requested_provider: str | None = None,
    ) -> None:
        self._provider_registry = provider_registry or DiscoveryProviderRegistry(providers)
        if provider_registry is not None:
            for name, provider in (providers or {}).items():
                self._provider_registry.register(name, provider)
        self._requested_provider = requested_provider

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        provider = self._provider_registry.provider_for(
            request,
            requested_provider=self._requested_provider,
        )
        if provider is None:
            return _failed_discovery_result(
                "not_selected",
                "discovery_provider_not_registered",
                f"No discovery provider registered for platform: {request.platform}.",
                metadata={"platform": request.platform, "limit": request.limit},
            )
        try:
            return provider.discover(request)
        except Exception as exc:  # pragma: no cover - final provider safety net
            return _failed_discovery_result(
                getattr(provider, "name", "unknown.discovery"),
                "discovery_provider_error",
                exc,
                metadata={"platform": request.platform, "limit": request.limit},
            )


def _failed_discovery_result(
    provider: str,
    code: str,
    error: object,
    *,
    notes: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> DiscoveryResult:
    return DiscoveryResult.failure(
        provider=provider,
        error_code=code,
        error_summary=sanitize_error_summary(error),
        source_notes=["discovery executor returned structured failure", *(notes or [])],
        metadata=dict(metadata or {}),
    )
