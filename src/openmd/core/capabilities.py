"""Read-only V1 capability projections backed by current registry declarations."""

from __future__ import annotations

from dataclasses import dataclass

from openmd.routing.loader import default_backend_chains, effective_routes, load_catalog


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
    card_id: str | None = None
    route_version: str | None = None
    overlay_applied: bool = False

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
            "card_id": self.card_id,
            "route_version": self.route_version,
            "overlay_applied": self.overlay_applied,
        }


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
    card_id: str | None = None,
    overlay_applied: bool = False,
) -> CapabilityRecord:
    planned = planned_provider
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
        card_id=card_id,
        route_version=load_catalog().version,
        overlay_applied=overlay_applied,
    )


def _project_capabilities(*, effective: bool = True) -> tuple[CapabilityRecord, ...]:
    catalog = load_catalog()
    chains = default_backend_chains(effective=effective)
    records: list[CapabilityRecord] = []
    for metadata in catalog.capability_metadata:
        source = metadata["route_source"]
        planned = chains[source][0] if source in chains else metadata["planned_provider"]
        binding = metadata.get("runtime_binding")
        runtime = planned if binding == "route" else binding
        card = catalog.capabilities.get(runtime or planned or "", {})
        records.append(_capability(
            metadata["source_id"], metadata["tier"], metadata["state"],
            tuple(metadata["input_kinds"]), tuple(metadata["source_exhausted_kinds"]), tuple(metadata["profiles"]),
            planned_provider=planned, runtime_provider=runtime,
            required_gates=tuple(card.get("gates", [])), cost_class=card.get("cost_class", "unknown"),
            card_id=card.get("card_id"),
            overlay_applied=effective and any(route["overlay_applied"] for route in effective_routes(source)),
        ))
    return tuple(records)


APPROVED_CAPABILITIES: tuple[CapabilityRecord, ...] = _project_capabilities(effective=False)


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
    for record in validate_capability_manifest(_project_capabilities()):
        if record.source_id == normalized:
            return record
    raise ValueError(f"source_unsupported: {source_id}")


def filter_capabilities(*, source_id: str | None = None) -> tuple[CapabilityRecord, ...]:
    current = validate_capability_manifest(_project_capabilities())
    if source_id is None:
        return current
    return tuple(record for record in current if record.source_id == source_id.strip().lower())
