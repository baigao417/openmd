"""Explicit, bounded route trials and append-only local route overrides.

Usable text without a reference is an availability observation, never a quality
ranking. This module does not schedule probes or modify bundled route data.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import platform
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from openmd.core.content_validation import validate_extraction_content
from openmd.core.local_config import checkout_root
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary, sanitize_url_text


TRIAL_SCHEMA = "openmd.route-trial/v1"
OVERLAY_SCHEMA = "openmd.route-overlay/v1"
SAMPLE_SCHEMA = "openmd.route-samples/v1"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES = 8
MAX_TEXT_CHARS = 2_000_000
_TOOL_OUTPUT_FAILURES = frozenset({
    "tool_artifact_missing", "tool_media_missing", "tool_json_invalid",
    "tool_subtitle_missing", "tool_content_empty", "tool_html_instead_of_markdown",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _root() -> Path:
    root = checkout_root()
    if root is None:
        raise ValueError("benchmark_checkout_required: supply explicit sample, ledger and artifact paths outside a checkout")
    return root


def _json_file(path: Path) -> Any:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("benchmark_file_missing_or_oversized")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("benchmark_json_invalid") from exc


def _read_jsonl(path: Path, schema: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("benchmark_ledger_oversized")
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError("benchmark_ledger_invalid_json") from exc
        if not isinstance(row, dict) or row.get("schema") != schema:
            raise ValueError("benchmark_ledger_schema_invalid")
        rows.append(row)
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("benchmark_ledger_oversized")
    data = json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n"
    # A single append preserves every prior trial/event. Malformed prior data
    # is rejected by callers before appending rather than silently truncated.
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
        stream.flush()


def load_samples(path: str | Path | None = None) -> tuple[dict[str, Any], ...]:
    sample_path = Path(path) if path is not None else _root() / "docs/benchmarks/routes/samples.json"
    value = _json_file(sample_path)
    if not isinstance(value, dict) or value.get("schema") != SAMPLE_SCHEMA or not isinstance(value.get("samples"), list):
        raise ValueError("benchmark_samples_schema_invalid")
    seen: set[str] = set()
    samples: list[dict[str, Any]] = []
    for raw in value["samples"]:
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k] for k in ("id", "source", "capability", "url")):
            raise ValueError("benchmark_sample_invalid")
        if raw["id"] in seen:
            raise ValueError("benchmark_sample_duplicate")
        seen.add(raw["id"])
        parsed = urlsplit(raw["url"])
        from openmd.adapters.local_audio.adapter import local_path_from_input

        local = local_path_from_input(raw["url"]) if raw["source"] in {"screenshot", "pdf"} else None
        local_sample = local is not None and local.is_file()
        if not local_sample and (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password):
            raise ValueError("benchmark_sample_url_invalid")
        if sanitize_url_text(raw["url"]) != raw["url"]:
            raise ValueError("benchmark_sample_private_url")
        sample = dict(raw)
        sample["_base_dir"] = str(sample_path.resolve().parent)
        samples.append(sample)
    return tuple(samples)


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in {"WEBVTT", ""} or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if " --> " in line or line.isdigit():
            continue
        line = re.sub(r"^\[\d{2}:\d{2}(?::\d{2})?(?:\s*-\s*\d{2}:\d{2}(?::\d{2})?)?\]\s*", "", line)
        lines.append(line)
    return "\n".join(lines)


def _quality(text: str, sample: Mapping[str, Any]) -> tuple[float | None, dict[str, Any]]:
    reference = sample.get("reference")
    if not reference:
        return None, {"kind": "unscored", "reason": "reference_missing", "reference_sha256": None}
    path = Path(reference)
    if not path.is_absolute():
        path = Path(str(sample["_base_dir"])) / path
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return None, {"kind": "unscored", "reason": "reference_unavailable", "reference_sha256": None}
    sha = _digest(path)
    if sample.get("reference_sha256") and sample["reference_sha256"] != sha:
        raise ValueError("benchmark_reference_hash_mismatch")
    ref = _plain(path.read_text(encoding="utf-8-sig"))
    hyp = _plain(text)
    metric = sample.get("metric", "paragraph_recall")
    evidence: dict[str, Any] = {"kind": metric, "reference_sha256": sha}
    if metric in {"cer", "wer"}:
        try:
            from rapidfuzz.distance import Levenshtein
        except ImportError:
            return None, {**evidence, "reason": "metric_dependency_missing"}
        ref = re.sub(r"[^\w\s]", "", ref.lower())
        hyp = re.sub(r"[^\w\s]", "", hyp.lower())
        if metric == "cer":
            if sample.get("language") == "zh":
                try:
                    from opencc import OpenCC
                except ImportError:
                    return None, {**evidence, "reason": "metric_dependency_missing_opencc"}
                ref, hyp = OpenCC("t2s").convert(ref), OpenCC("t2s").convert(hyp)
            a, b = "".join(ref.split()), "".join(hyp.split())
        else:
            a, b = ref.split(), hyp.split()
        if not a:
            return None, {**evidence, "reason": "reference_empty"}
        errors = Levenshtein.distance(a, b)
        rate = errors / len(a)
        return max(0.0, 1.0 - rate), {**evidence, "error_rate": round(rate, 6), "reference_units": len(a), "errors": errors}
    if metric != "paragraph_recall":
        raise ValueError("benchmark_metric_unsupported")
    normalized = lambda s: " ".join(s.lower().split())
    paragraphs = [normalized(p) for p in re.split(r"\n\s*\n|\n", ref) if normalized(p)]
    haystack = normalized(hyp)
    if not paragraphs:
        return None, {**evidence, "reason": "reference_empty"}
    recalled = sum(len(p) for p in paragraphs if p in haystack)
    total = sum(map(len, paragraphs))
    recall = recalled / total
    # Exact reference-paragraph coverage penalizes excess text without an
    # unbounded quadratic diff over long articles. This is a content metric,
    # not CER/WER and not a substitute for independently reviewed references.
    precision = min(1.0, recalled / max(1, len(haystack)))
    return recall * precision, {**evidence, "paragraph_recall": round(recall, 6), "text_precision": round(precision, 6)}


def _sample_fingerprint(sample: Mapping[str, Any]) -> str:
    identity = {key: value for key, value in sample.items() if not key.startswith("_")}
    if sample.get("source") in {"pdf", "screenshot"}:
        from openmd.adapters.local_audio.adapter import local_path_from_input

        local = local_path_from_input(str(sample.get("url", "")))
        if local is not None and local.is_file():
            identity["actual_input_sha256"] = _digest(local)
    if sample.get("reference"):
        path = Path(str(sample["reference"]))
        if not path.is_absolute():
            path = Path(str(sample["_base_dir"])) / path
        identity["actual_reference_sha256"] = _digest(path) if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES else None
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def score_trials(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Score measured quality at 80%, log throughput at 20%, within one sample."""
    result = [dict(row) for row in rows]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in result:
        valid = row.get("validation", {}).get("ok") is True
        quality = row.get("quality")
        row["score"] = 0.0 if not valid else None
        row["speed"] = None
        row["rankable"] = False
        if valid and isinstance(quality, (int, float)) and not isinstance(quality, bool):
            if not math.isfinite(quality) or not 0 <= quality <= 1:
                raise ValueError("benchmark_quality_invalid")
            seconds = row.get("seconds")
            if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("benchmark_duration_invalid")
            reference_sha = row.get("quality_evidence", {}).get("reference_sha256")
            if reference_sha:
                groups[(row.get("source"), row.get("capability"), row.get("sample_id"), reference_sha, row.get("quality_evidence", {}).get("kind"))].append(row)
    for group in groups.values():
        speeds = [math.log(1 / row["seconds"]) for row in group]
        low, high = min(speeds), max(speeds)
        for row, speed in zip(group, speeds):
            normalized = (speed - low) / (high - low) if high > low else 1.0
            row["speed"] = round(normalized, 6)
            row["score"] = round(0.8 * row["quality"] + 0.2 * normalized, 6)
            row["rankable"] = True
    return result


def _execute_backend(candidate: str, sample: dict[str, Any], options: Any) -> TranscriptResult:
    from openmd.adapters.registry import create_default_registry
    from openmd.backends.executor import BackendExecutor
    from openmd.backends.registry import create_runtime_backend_provider_registry

    adapter = create_default_registry().get_adapter(sample["source"])
    items = adapter.resolve_items(sample["url"])
    if len(items) != 1:
        raise ValueError("benchmark_single_item_required")
    selected = replace(options, backend_override=candidate)
    registry = create_runtime_backend_provider_registry(sample["source"], selected)
    executor = BackendExecutor(provider_registry=registry)
    identity = executor.resolve_item_identity(items[0], (candidate,))
    if not identity.ok:
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code=identity.error_code, error_summary=identity.error_summary)
    return executor.extract(identity.item, (candidate,), allow_fallback=False)


def _trial_artifacts(folder: Path, result: TranscriptResult, summary: dict[str, Any]) -> list[dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=False)
    if result.has_usable_text:
        (folder / "content.md").write_text(result.text, encoding="utf-8", newline="\n")
    (folder / "result.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True), encoding="utf-8", newline="\n")
    return [{"path": str(path.resolve()), "sha256": _digest(path), "bytes": path.stat().st_size} for path in sorted(folder.iterdir())]


def run_benchmark(
    *, source: str, capability: str, candidates: Sequence[str] | None,
    options: Any, ledger_path: str | Path | None = None,
    samples_path: str | Path | None = None, artifact_dir: str | Path | None = None,
    sample_id: str | None = None,
    executor: Callable[[str, dict[str, Any], Any], TranscriptResult] | None = None,
) -> dict[str, Any]:
    """Run one fixed sample once per candidate using explicit runtime gates."""
    from openmd.routing.loader import load_catalog

    catalog = load_catalog()
    routes = [r for r in catalog.routes if r["source"] == source and r["capability"] == capability]
    if len(routes) != 1:
        raise ValueError("benchmark_route_not_found")
    chain = routes[0]["chain"]
    selected = list(dict.fromkeys(candidates if candidates is not None else chain))
    if not selected or len(selected) > MAX_CANDIDATES or any(c not in chain and (c not in catalog.capabilities or source not in catalog.capabilities[c].get("sources", []) or catalog.capabilities[c].get("capability") != capability) for c in selected):
        raise ValueError("benchmark_candidates_invalid")
    matching = [s for s in load_samples(samples_path) if s["source"] == source and s["capability"] == capability and (sample_id is None or s["id"] == sample_id)]
    if not matching:
        raise ValueError("benchmark_sample_not_found")
    sample = matching[0]
    sample_sha256 = _sample_fingerprint(sample)
    gate_options = {
        "network": "enable_network_backends", "browser": "enable_browser_backends",
        "media_download": "enable_media_download_backends", "asr": "enable_asr_backends",
        "cloud": "enable_cloud_backends",
    }
    # Validate every candidate before starting any work or writing artifacts.
    for candidate in selected:
        cap = catalog.capabilities[candidate]
        for gate in cap.get("gates", []):
            if gate == "local_compute":
                continue
            if gate == "account" or gate not in gate_options or not getattr(options, gate_options[gate], False):
                raise ValueError(f"benchmark_gate_required: {candidate}: {gate}")
        if cap.get("requires_login"):
            raise ValueError("benchmark_account_candidate_blocked")
    ledger = Path(ledger_path) if ledger_path is not None else _root() / f"docs/benchmarks/routes/trials-{datetime.now(timezone.utc):%Y-%m}.jsonl"
    _read_jsonl(ledger, TRIAL_SCHEMA)
    artifacts = Path(artifact_dir) if artifact_dir is not None else _root() / "out/route-benchmarks"
    rows: list[dict[str, Any]] = []
    for candidate in selected:
        cap = catalog.capabilities[candidate]
        card = catalog.cards[cap["card_id"]]
        started = time.perf_counter()
        try:
            result = (executor or _execute_backend)(candidate, sample, options)
            if not isinstance(result, TranscriptResult):
                raise ValueError("benchmark_provider_invalid_result")
            result = validate_extraction_content(result)
        except Exception as exc:
            result = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code="benchmark_provider_failed", error_summary=sanitize_error_summary(exc))
        elapsed = max(time.perf_counter() - started, 0.000001)
        if len(result.text) > MAX_TEXT_CHARS:
            result = replace(result, text="", status="failed", source_level="partial_extraction", error_code="benchmark_output_oversized")
        # The measured hypothesis must be exactly the retained, sanitized text
        # so a reviewer can reproduce quality from the evidence artifact.
        result = replace(result, text=sanitize_url_text(result.text))
        tool_run = result.metadata.get("tool_run", {})
        tool_version = tool_run.get("tool_version") or card.get("version")
        tool_version_verified = tool_run.get("tool_version_verified") is True
        trial_id = uuid.uuid4().hex
        validation = {"ok": result.status == "verified" and bool(result.text.strip()), "code": result.error_code or (None if result.status == "verified" and result.text.strip() else "benchmark_content_not_verified")}
        quality, quality_evidence = _quality(result.text, sample) if validation["ok"] else (0.0, {"kind": "failed", "reference_sha256": None})
        summary = {"schema": TRIAL_SCHEMA, "trial_id": trial_id, "ts": _now(), "source": source, "capability": capability, "candidate": candidate, "tool": cap["card_id"], "tool_version": tool_version, "tool_version_verified": tool_version_verified, "sample_id": sample["id"], "sample_sha256": sample_sha256, "validation": validation, "status": result.status, "source_level": result.source_level, "backend": result.backend, "chars": len(result.text)}
        if result.metadata.get("pages"):
            from openmd.core.secrets import sanitize_document_metadata

            summary["document_metadata"] = sanitize_document_metadata({"pages": result.metadata["pages"]})
        evidence_files = _trial_artifacts(artifacts / trial_id, result, summary)
        row = {**summary, "card_id": cap["card_id"], "route_version": catalog.version, "sample_url": sanitize_url_text(sample["url"]), "argv_redacted": cap.get("argv", []), "exit_code": tool_run.get("exit_code"), "seconds": round(elapsed, 6), "files": tool_run.get("files", {}), "artifacts": evidence_files, "quality": quality, "quality_evidence": quality_evidence, "host_fingerprint": hashlib.sha256(f"{platform.system()}:{platform.machine()}:{platform.python_version()}".encode()).hexdigest()[:16]}
        rows.append(row)
    scored = score_trials(rows)
    for row in scored:
        _append_jsonl(ledger, row)
    ranked = sorted((r for r in scored if r["rankable"]), key=lambda r: r["score"], reverse=True)
    return {"schema": "openmd.route-benchmark-report/v1", "source": source, "capability": capability, "sample_id": sample["id"], "ledger": str(ledger.resolve()), "rows": scored, "ranking": [{"candidate": r["candidate"], "score": r["score"]} for r in ranked], "quality_note": "No reference means unscored availability evidence; scores compare only the same sample/reference."}


def _overlay_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    from openmd.routing.loader import overlay_file
    return Path(overlay_file())


def apply_overlay(routes: Sequence[Mapping[str, Any]], overlay_path: str | Path | None) -> list[dict[str, Any]]:
    """Replay local events and return fresh effective route dictionaries."""
    output = [{**route, "chain": list(route["chain"]), "overlay_applied": False} for route in routes]
    if overlay_path is None:
        return output
    active: dict[tuple[str, str], bool] = {}
    for event in _read_jsonl(Path(overlay_path), OVERLAY_SCHEMA):
        if event.get("op") not in {"demote", "restore"} or not all(isinstance(event.get(k), str) and event[k] for k in ("source", "capability_id", "ts")):
            raise ValueError("route_overlay_event_invalid")
        if not any(r["source"] == event["source"] and event["capability_id"] in r["chain"] for r in output):
            raise ValueError("route_overlay_candidate_unknown")
        active[(event["source"], event["capability_id"])] = event["op"] == "demote"
    for route in output:
        chain = route["chain"]
        demoted = [c for c in chain if active.get((route["source"], c), False)]
        route["chain"] = [c for c in chain if c not in demoted] + demoted
        route["overlay_applied"] = bool(demoted)
    return output


def _known_candidate(capability_id: str, source: str) -> None:
    from openmd.routing.loader import load_catalog
    if not any(r["source"] == source and capability_id in r["chain"] for r in load_catalog().routes):
        raise ValueError("route_overlay_candidate_unknown")


def _verify_trial_artifacts(row: Mapping[str, Any]) -> None:
    artifacts = row.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 16:
        raise ValueError("route_evidence_artifacts_missing")
    summaries: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("route_evidence_artifact_invalid")
        path = Path(artifact["path"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact.get("bytes") or _digest(path) != artifact.get("sha256"):
            raise ValueError("route_evidence_artifact_changed")
        if path.name == "result.json":
            summaries.append(_json_file(path))
    keys = ("schema", "trial_id", "ts", "source", "capability", "candidate", "tool", "tool_version", "tool_version_verified", "sample_id", "sample_sha256", "validation", "status", "source_level", "backend", "chars")
    if len(summaries) != 1 or any(summaries[0].get(k) != row.get(k) for k in keys):
        raise ValueError("route_evidence_summary_mismatch")


def demote(
    capability_id: str, *, source: str, evidence_ids: Sequence[str], ledger_path: str | Path,
    overlay_path: str | Path | None = None, note: str = "",
) -> dict[str, Any]:
    """Demote only after two reproducible, artifact-backed tool failures."""
    _known_candidate(capability_id, source)
    ids = list(dict.fromkeys(evidence_ids))
    if len(ids) < 2:
        raise ValueError("route_evidence_two_trials_required")
    rows = _read_jsonl(Path(ledger_path), TRIAL_SCHEMA)
    selected: list[dict[str, Any]] = []
    observed_times: set[datetime] = set()
    for trial_id in ids:
        matches = [r for r in rows if r.get("trial_id") == trial_id]
        if len(matches) != 1:
            raise ValueError("route_evidence_trial_not_unique")
        row = matches[0]
        if row.get("source") != source or row.get("candidate") != capability_id or row.get("status") != "failed" or row.get("validation", {}).get("ok") is not False:
            raise ValueError("route_evidence_failed_candidate_required")
        code = row.get("validation", {}).get("code")
        # Exit codes/timeouts can reflect networking or machine health; ASR and
        # harness failures belong to different components. Only an explicit
        # tool-output contract failure supports this automatic attribution.
        if code not in _TOOL_OUTPUT_FAILURES:
            raise ValueError("route_evidence_not_tool_failure")
        version = row.get("tool_version")
        if row.get("tool_version_verified") is not True or not isinstance(version, str) or version.strip().lower() in {"", "unknown", "unlocked", "unverified"}:
            raise ValueError("route_evidence_version_required")
        try:
            parsed_time = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("route_evidence_timestamp_invalid") from exc
        if parsed_time.tzinfo is None:
            raise ValueError("route_evidence_timestamp_invalid")
        if parsed_time in observed_times:
            raise ValueError("route_evidence_not_comparable")
        observed_times.add(parsed_time)
        _verify_trial_artifacts(row)
        selected.append(row)
    group_keys = ("sample_id", "sample_sha256", "sample_url", "tool_version", "tool", "capability", "route_version")
    if any(len({r.get(k) for r in selected}) != 1 for k in group_keys) or len({r["ts"] for r in selected}) != len(selected):
        raise ValueError("route_evidence_not_comparable")
    path = _overlay_path(overlay_path)
    _read_jsonl(path, OVERLAY_SCHEMA)
    event = {"schema": OVERLAY_SCHEMA, "op": "demote", "source": source, "capability_id": capability_id, "evidence": ids, "ts": _now(), "note": sanitize_error_summary(note) or "Two comparable tool failures with verified artifacts."}
    _append_jsonl(path, event)
    return event


def restore(capability_id: str, *, source: str, overlay_path: str | Path | None = None, note: str = "") -> dict[str, Any]:
    """Restore the bundled position by appending an explicit local event."""
    _known_candidate(capability_id, source)
    path = _overlay_path(overlay_path)
    prior = _read_jsonl(path, OVERLAY_SCHEMA)
    if not any(r.get("op") == "demote" and r.get("source") == source and r.get("capability_id") == capability_id for r in prior):
        raise ValueError("route_overlay_no_demotion_to_restore")
    event = {"schema": OVERLAY_SCHEMA, "op": "restore", "source": source, "capability_id": capability_id, "evidence": [], "ts": _now(), "note": sanitize_error_summary(note) or "Explicit restore of bundled route position."}
    _append_jsonl(path, event)
    return event
