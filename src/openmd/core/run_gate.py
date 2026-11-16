"""Source-bound run permissions. This is a wrapper contract, not a sandbox."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = 'openmd.run-gate/v1'
ACTORS = {'user', 'dispatcher', 'worker'}


def validate_gate(gate: dict, authority: dict, *, action: str, target: Path) -> None:
    if gate.get('schema') != SCHEMA or authority.get('schema') != SCHEMA:
        raise ValueError('gate_schema_invalid')
    if authority.get('actor') != 'user' or not authority.get('authority_source') or not authority.get('evidence_ref'):
        raise ValueError('user_authority_missing')
    digest = hashlib.sha256(json.dumps(authority, sort_keys=True).encode()).hexdigest()
    if gate.get('authority_sha256') != digest or gate.get('actor') not in ACTORS:
        raise ValueError('authority_identity_mismatch')
    if not gate.get('goal') or not gate.get('authority_source') or not gate.get('evidence_ref'):
        raise ValueError('gate_provenance_missing')
    if gate.get('unknown') is not False or authority.get('unknown') is not False:
        raise ValueError('gate_unknown')
    allowed = gate.get('allowed_side_effects', [])
    if not isinstance(allowed, list) or not set(allowed) <= set(authority.get('allowed_side_effects', [])):
        raise ValueError('authority_escalation')
    if action not in allowed:
        raise ValueError('action_not_authorized')
    if gate.get('supersedes') and gate['actor'] != 'user':
        raise ValueError('worker_or_dispatcher_cannot_supersede_user')
    resolved = target.resolve()
    roots = [Path(p).resolve() for p in gate.get('scope', [])]
    parent_roots = [Path(p).resolve() for p in authority.get('scope', [])]
    if not roots or not all(any(p == q or p.is_relative_to(q) for q in parent_roots) for p in roots):
        raise ValueError('scope_escalation')
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ValueError('target_out_of_scope')
