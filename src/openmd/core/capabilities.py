"""Read-only V1 capability projections backed by current registry declarations."""

from __future__ import annotations

from dataclasses import dataclass

from openmd.adapters.registry import DEFAULT_BACKENDS


CAPABILITY_TIERS = frozenset({"A", "B", "C", "D"})
CAPABILITY_STATES = frozenset({"ready", "partial", "blocked", "experimental", "paused"})
BINDING_STATES = frozenset({"implemented", "unimplemented"})


@dataclass(frozen=True)
class CapabilityRecord:
    source_id: str
    tier: str
    state: str
    input_kinds: tuple[str, ...]
    source_exhausted_kinds: tuple[str, ...]
    profiles: tuple[str, ...]
    planned_provider: str | None
    runtime_provider: str | None
    binding_state: str
    required_gates: tuple[str, ...]
    cost_class: str
    execution_ready: bool
    blocked_reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "tier": self.tier,
            "state": self.state,
            "input_kinds": list(self.input_kinds),
            "source_exhausted_kinds": list(self.source_exhausted_kinds),
            "profiles": list(self.profiles),
            "planned_provider": self.planned_provider,
            "runtime_provider": self.runtime_provider,
            "binding_state": self.binding_state,
            "required_gates": list(self.required_gates),
            "cost_class": self.cost_class,
            "execution_ready": self.execution_ready,
            "blocked_reason": self.blocked_reason,
        }


def _registry_provider(platform: str) -> str:
    return DEFAULT_BACKENDS[platform][0]


def _capability(
    source_id: str,
    tier: str,
    state: str,
    input_kinds: tuple[str, ...],
    source_exhausted_kinds: tuple[str, ...],
    profiles: tuple[str, ...],
    *,
    platform: str | None = None,
    planned_provider: str | None = None,
    runtime_provider: str | None = None,
    required_gates: tuple[str, ...] = (),
    cost_class: str = "unknown",
) -> CapabilityRecord:
    planned = planned_provider or _registry_provider(platform or source_id)
    binding_state = "implemented" if runtime_provider is not None else "unimplemented"
    blocked_reason: str | None = None
    if state == "paused":
        blocked_reason = "capability_paused"
    elif binding_state == "unimplemented":
        blocked_reason = "runtime_provider_unimplemented"
    elif required_gates:
        blocked_reason = "required_gate_missing"
    return CapabilityRecord(
        source_id=source_id,
        tier=tier,
        state=state,
        input_kinds=input_kinds,
        source_exhausted_kinds=source_exhausted_kinds,
        profiles=profiles,
        planned_provider=planned,
        runtime_provider=runtime_provider,
        binding_state=binding_state,
        required_gates=required_gates,
        cost_class=cost_class,
        execution_ready=state not in {"paused", "blocked"} and binding_state == "implemented" and not required_gates,
        blocked_reason=blocked_reason,
    )


# Runtime providers below are identifiers registered by the current provider modules.
# A missing binding stays explicit instead of treating a route declaration as executable.
APPROVED_CAPABILITIES: tuple[CapabilityRecord, ...] = (
    _capability("bilibili", "A", "partial", ("single", "collection"), ("collection",), ("media",), platform="bilibili", runtime_provider="bilibili-harvest.core", required_gates=("network",), cost_class="unknown"),
    _capability("youtube", "A", "partial", ("single", "collection"), ("collection",), ("media",), platform="youtube", runtime_provider="youtube-transcript-api", required_gates=("network",), cost_class="unknown"),
    _capability("weixin", "A", "partial", ("single", "collection"), ("collection",), ("article",), platform="weixin", runtime_provider="weixin.article.local", required_gates=("network",), cost_class="unknown"),
    _capability("xiaoyuzhou", "A", "partial", ("single", "collection"), ("collection",), ("media",), platform="xiaoyuzhou", runtime_provider="opencli.xiaoyuzhou.transcript", required_gates=("browser",), cost_class="unknown"),
    _capability("douyin", "A", "partial", ("single", "collection"), ("collection",), ("media",), platform="douyin", runtime_provider="douyin.transcript", required_gates=("browser",), cost_class="unknown"),
    _capability("tiktok", "C", "paused", ("single",), (), ("media",), platform="tiktok"),
    _capability("xiaohongshu", "A", "partial", ("single", "list"), ("list",), ("article", "media"), platform="xiaohongshu", runtime_provider="xiaohongshu.composite", required_gates=("browser",), cost_class="unknown"),
    _capability("xiaohongshu_creator", "B", "blocked", ("collection",), (), ("article", "media"), platform="xiaohongshu"),
    _capability("xiaoe", "A", "partial", ("single", "collection"), ("collection",), ("media",), platform="xiaoe", runtime_provider="opencli.xiaoe.transcript", required_gates=("browser",), cost_class="unknown"),
    _capability("x", "B", "partial", ("single", "list"), (), ("article",), platform="x", runtime_provider="opencli.twitter.thread", required_gates=("browser",), cost_class="unknown"),
    _capability("web_article", "B", "ready", ("single", "list"), (), ("article",), platform="web_article", runtime_provider="web_article.trafilatura", required_gates=("network",), cost_class="unknown"),
    _capability("feishu", "B", "partial", ("single", "list"), (), ("article", "document"), platform="feishu", runtime_provider="lark-cli.docs.fetch", required_gates=("network", "account"), cost_class="unknown"),
    _capability("generic_media", "B", "partial", ("single", "list"), (), ("media",), platform="generic_media", required_gates=("asr",), cost_class="local_compute"),
    _capability("local_audio", "A", "ready", ("file", "folder"), ("folder",), ("media",), planned_provider="asr.faster_whisper", runtime_provider="asr.faster_whisper", required_gates=("asr",), cost_class="local_compute"),
    _capability("local_video", "B", "partial", ("file", "folder"), ("folder",), ("media",), planned_provider="asr.fallback", runtime_provider="asr.faster_whisper", required_gates=("asr",), cost_class="local_compute"),
    _capability("tutorial_video", "C", "experimental", ("file", "single"), (), ("media",), planned_provider="asr.fallback", runtime_provider="asr.faster_whisper", required_gates=("asr",), cost_class="local_compute"),
    _capability("pdf_office", "A", "partial", ("file", "folder"), ("folder",), ("document",), platform="pdf", runtime_provider="pdf.markitdown", required_gates=("local_compute",), cost_class="local_compute"),
    _capability("screenshot", "B", "partial", ("file", "folder"), ("folder",), ("document",), platform="screenshot", runtime_provider="ocr.rapidocr", required_gates=("local_compute",), cost_class="local_compute"),
    _capability("wechat_moments", "B", "partial", ("collection",), ("collection",), ("conversation",), platform="wechat_moments", required_gates=("local_export",), cost_class="none"),
    _capability("wechat_group", "B", "partial", ("collection",), ("collection",), ("conversation",), platform="wechat_group", required_gates=("local_export",), cost_class="none"),
    _capability("qq_group", "B", "paused", ("collection",), (), ("conversation",), platform="qq_group"),
)


def validate_capability_manifest(
    records: tuple[CapabilityRecord, ...] = APPROVED_CAPABILITIES,
) -> tuple[CapabilityRecord, ...]:
    seen: set[str] = set()
    for record in records:
        if not record.source_id or record.source_id in seen:
            raise ValueError("capability source_id is duplicated or empty")
        seen.add(record.source_id)
        if record.tier not in CAPABILITY_TIERS:
            raise ValueError(f"invalid capability tier: {record.source_id}")
        if record.state not in CAPABILITY_STATES:
            raise ValueError(f"invalid capability state: {record.source_id}")
        if record.binding_state not in BINDING_STATES:
            raise ValueError(f"invalid binding state: {record.source_id}")
        if not record.input_kinds or not record.profiles:
            raise ValueError(f"capability fields missing: {record.source_id}")
        if record.binding_state == "implemented" and record.runtime_provider is None:
            raise ValueError(f"runtime provider missing: {record.source_id}")
        if record.binding_state == "unimplemented" and record.runtime_provider is not None:
            raise ValueError(f"unexpected runtime provider: {record.source_id}")
    return tuple(sorted(records, key=lambda record: record.source_id))


NORMALIZED_CAPABILITIES = validate_capability_manifest()


def get_capability(source_id: str) -> CapabilityRecord:
    normalized = source_id.strip().lower()
    for record in NORMALIZED_CAPABILITIES:
        if record.source_id == normalized:
            return record
    raise ValueError(f"source_unsupported: {source_id}")


def filter_capabilities(*, source_id: str | None = None) -> tuple[CapabilityRecord, ...]:
    if source_id is None:
        return NORMALIZED_CAPABILITIES
    return tuple(record for record in NORMALIZED_CAPABILITIES if record.source_id == source_id.strip().lower())
