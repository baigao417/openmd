from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

from openmd.core.models import TranscriptResult
from openmd.routing.lifecycle import (
    OVERLAY_SCHEMA,
    _quality,
    apply_overlay,
    demote,
    load_samples,
    restore,
    run_benchmark,
    score_trials,
)


@pytest.fixture
def catalog(monkeypatch):
    value = types.SimpleNamespace(
        version="unit-v1",
        cards={"primary": {"version": "1.0"}, "fallback": {"version": "2.0"}},
        capabilities={
            "primary.content": {"card_id": "primary", "gates": ["network"], "argv": ["{url}"]},
            "fallback.content": {"card_id": "fallback", "gates": ["network"], "argv": ["{url}"]},
        },
        routes=({"source": "web_article", "capability": "content", "chain": ["primary.content", "fallback.content"]},),
    )
    fake_loader = types.ModuleType("openmd.routing.loader")
    fake_loader.load_catalog = lambda: value
    monkeypatch.setitem(sys.modules, "openmd.routing.loader", fake_loader)
    return value


@pytest.fixture
def inputs(tmp_path):
    sample = tmp_path / "samples.json"
    sample.write_text(json.dumps({"schema": "openmd.route-samples/v1", "samples": [{"id": "sample", "source": "web_article", "capability": "content", "url": "https://example.org/article"}]}), encoding="utf-8")
    return {"source": "web_article", "capability": "content", "candidates": ["primary.content"], "options": types.SimpleNamespace(enable_network_backends=True), "ledger_path": tmp_path / "trials.jsonl", "samples_path": sample, "artifact_dir": tmp_path / "artifacts"}


def _result(candidate, sample, options):
    return TranscriptResult(text="The source paragraph remains intact.\n", status="verified", source_level="native_text", backend=candidate, metadata={"tool_run": {"tool_version": "1.0", "tool_version_verified": True, "exit_code": 0}})


def _failure(candidate, sample, options):
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code="tool_json_invalid", metadata={"tool_run": {"tool_version": "1.0", "tool_version_verified": True, "exit_code": 0}})


def test_success_without_reference_is_availability_not_quality(catalog, inputs):
    report = run_benchmark(**inputs, executor=_result)
    row = report["rows"][0]
    assert row["validation"]["ok"] is True
    assert row["quality"] is None and row["score"] is None
    assert report["ranking"] == []
    artifact = next(p for p in row["artifacts"] if Path(p["path"]).name == "content.md")
    assert Path(artifact["path"]).read_text(encoding="utf-8") == _result("", {}, None).text
    assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    ledger = Path(inputs["ledger_path"]).read_text(encoding="utf-8")
    assert "source paragraph remains" not in ledger
    assert row["trial_id"] in ledger


def test_all_candidates_checked_before_any_network_or_files(catalog, inputs):
    inputs["candidates"] = ["primary.content", "fallback.content"]
    catalog.capabilities["fallback.content"]["gates"] = ["account"]
    called = []
    with pytest.raises(ValueError, match="benchmark_gate_required"):
        run_benchmark(**inputs, executor=lambda *args: called.append(args))
    assert called == []
    assert not Path(inputs["ledger_path"]).exists()
    assert not Path(inputs["artifact_dir"]).exists()


def test_browser_error_page_cannot_be_success_evidence(catalog, inputs):
    def browser_failure(candidate, sample, options):
        return TranscriptResult(text="This site can’t be reached\nERR_CONNECTION_RESET\nReload", title="This site can’t be reached", status="verified", source_level="browser_rendered", backend=candidate)
    row = run_benchmark(**inputs, executor=browser_failure)["rows"][0]
    assert row["validation"] == {"ok": False, "code": "browser_error_page"}
    assert row["score"] == 0
    assert all(Path(p["path"]).name != "content.md" for p in row["artifacts"])


def test_scoring_never_compares_different_samples_or_reference_sets():
    base = {"source": "x", "capability": "content", "sample_id": "a", "validation": {"ok": True}, "quality": 0.5, "quality_evidence": {"reference_sha256": "ref-a", "kind": "paragraph_recall"}, "seconds": 10}
    rows = [copy.deepcopy(base), {**copy.deepcopy(base), "seconds": 1}, {**copy.deepcopy(base), "sample_id": "b", "seconds": 0.0001}, {**copy.deepcopy(base), "quality_evidence": {"reference_sha256": "ref-b", "kind": "paragraph_recall"}, "seconds": 100000}]
    scored = score_trials(rows)
    assert scored[0]["score"] == 0.4
    assert scored[1]["score"] == 0.6
    assert scored[2]["score"] == scored[3]["score"] == 0.6
    assert all("score" not in row for row in rows)


def test_demote_requires_two_matching_failed_trials_and_preserves_history(catalog, inputs, tmp_path):
    first = run_benchmark(**inputs, executor=_failure)["rows"][0]
    second = run_benchmark(**inputs, executor=_failure)["rows"][0]
    overlay = tmp_path / "overlay.jsonl"
    with pytest.raises(ValueError, match="two_trials"):
        demote("primary.content", source="web_article", evidence_ids=[first["trial_id"]], ledger_path=inputs["ledger_path"], overlay_path=overlay)
    demote("primary.content", source="web_article", evidence_ids=[first["trial_id"], second["trial_id"]], ledger_path=inputs["ledger_path"], overlay_path=overlay)
    before = overlay.read_bytes()
    active = apply_overlay(catalog.routes, overlay)
    assert active[0]["chain"] == ["fallback.content", "primary.content"]
    assert active[0]["overlay_applied"]
    restore("primary.content", source="web_article", overlay_path=overlay)
    assert overlay.read_bytes().startswith(before)
    restored = apply_overlay(catalog.routes, overlay)
    assert restored[0]["chain"] == catalog.routes[0]["chain"]
    assert restored[0]["overlay_applied"] is False
    assert catalog.routes[0]["chain"] == ["primary.content", "fallback.content"]


def test_changed_artifacts_prevent_demotion(catalog, inputs, tmp_path):
    rows = [run_benchmark(**inputs, executor=_failure)["rows"][0] for _ in range(2)]
    Path(rows[0]["artifacts"][0]["path"]).write_text("{}", encoding="utf-8")
    overlay = tmp_path / "overlay.jsonl"
    with pytest.raises(ValueError, match="artifact_changed"):
        demote("primary.content", source="web_article", evidence_ids=[r["trial_id"] for r in rows], ledger_path=inputs["ledger_path"], overlay_path=overlay)
    assert not overlay.exists()


def test_missing_permission_or_tool_is_not_evidence_for_demotion(catalog, inputs, tmp_path):
    def blocked(candidate, sample, options):
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code="backend_not_registered", metadata={"tool_run": {"tool_version": "1.0"}})
    rows = [run_benchmark(**inputs, executor=blocked)["rows"][0] for _ in range(2)]
    with pytest.raises(ValueError, match="not_tool_failure"):
        demote("primary.content", source="web_article", evidence_ids=[r["trial_id"] for r in rows], ledger_path=inputs["ledger_path"], overlay_path=tmp_path / "overlay.jsonl")


def test_same_trial_repeated_does_not_satisfy_two_observations(catalog, inputs, tmp_path):
    row = run_benchmark(**inputs, executor=_failure)["rows"][0]
    with pytest.raises(ValueError, match="two_trials"):
        demote("primary.content", source="web_article", evidence_ids=[row["trial_id"], row["trial_id"]], ledger_path=inputs["ledger_path"], overlay_path=tmp_path / "overlay.jsonl")


def test_declared_version_without_installed_version_verification_cannot_demote(catalog, inputs, tmp_path):
    def declared_only(candidate, sample, options):
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code="tool_json_invalid")
    rows = [run_benchmark(**inputs, executor=declared_only)["rows"][0] for _ in range(2)]
    with pytest.raises(ValueError, match="version_required"):
        demote("primary.content", source="web_article", evidence_ids=[r["trial_id"] for r in rows], ledger_path=inputs["ledger_path"], overlay_path=tmp_path / "overlay.jsonl")


def test_reused_sample_id_with_changed_link_cannot_demote(catalog, inputs, tmp_path):
    first = run_benchmark(**inputs, executor=_failure)["rows"][0]
    path = Path(inputs["samples_path"])
    data = json.loads(path.read_text())
    data["samples"][0]["url"] = "https://example.org/a-different-article"
    path.write_text(json.dumps(data), encoding="utf-8")
    second = run_benchmark(**inputs, executor=_failure)["rows"][0]
    with pytest.raises(ValueError, match="not_comparable"):
        demote("primary.content", source="web_article", evidence_ids=[first["trial_id"], second["trial_id"]], ledger_path=inputs["ledger_path"], overlay_path=tmp_path / "overlay.jsonl")


def test_overlay_unknown_candidate_and_malformed_json_fail_closed(tmp_path):
    routes = [{"source": "x", "chain": ["a", "b"]}]
    overlay = tmp_path / "overlay.jsonl"
    overlay.write_text(json.dumps({"schema": OVERLAY_SCHEMA, "op": "demote", "source": "x", "capability_id": "missing", "ts": "2026-11-07T12:00:00+00:00"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_unknown"):
        apply_overlay(routes, overlay)
    overlay.write_text("{incomplete", encoding="utf-8")
    with pytest.raises(ValueError, match="ledger_invalid_json"):
        apply_overlay(routes, overlay)


def test_sample_private_query_rejected_and_reference_hash_checked(catalog, inputs, tmp_path):
    sample_path = Path(inputs["samples_path"])
    data = json.loads(sample_path.read_text())
    data["samples"][0]["url"] += "?token=private"
    sample_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="private_url"):
        load_samples(sample_path)
    data["samples"][0]["url"] = "https://example.org/article"
    reference = tmp_path / "reference.txt"
    reference.write_text("source paragraph", encoding="utf-8")
    data["samples"][0].update(reference="reference.txt", reference_sha256="bad")
    sample_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="reference_hash_mismatch"):
        run_benchmark(**inputs, executor=_result)


def test_references_enable_measured_quality_and_append_new_trials(catalog, inputs, tmp_path):
    sample_path = Path(inputs["samples_path"])
    data = json.loads(sample_path.read_text())
    reference = tmp_path / "reference.txt"
    reference.write_text("The source paragraph remains intact.", encoding="utf-8")
    data["samples"][0].update(reference="reference.txt", metric="paragraph_recall")
    sample_path.write_text(json.dumps(data), encoding="utf-8")
    report = run_benchmark(**inputs, executor=_result)
    row = report["rows"][0]
    assert row["quality"] == 1
    assert row["quality_evidence"]["reference_sha256"]
    assert report["ranking"] == [{"candidate": "primary.content", "score": 1.0}]
    before = Path(inputs["ledger_path"]).read_bytes()
    run_benchmark(**inputs, executor=_result)
    assert Path(inputs["ledger_path"]).read_bytes().startswith(before)


@pytest.mark.parametrize("metric", ["cer", "wer"])
def test_point_and_range_timestamps_have_same_quality_and_text_errors_count(monkeypatch, tmp_path, metric):
    # A narrow metric stand-in checks the actual normalized arguments without
    # requiring optional ASR scoring packages in the controller environment.
    def single_substitution_distance(a, b):
        assert len(a) == len(b)
        differences = sum(left != right for left, right in zip(a, b))
        assert differences in {0, 1}
        return differences

    distance = types.ModuleType("rapidfuzz.distance")
    distance.Levenshtein = types.SimpleNamespace(distance=single_substitution_distance)
    monkeypatch.setitem(sys.modules, "rapidfuzz.distance", distance)
    reference = tmp_path / "reference.txt"
    reference.write_text("The cat sat.\nIt is here.", encoding="utf-8")
    sample = {"reference": str(reference), "metric": metric}
    point = "[00:00:01] The cat sat.\n[00:00:05] It is here."
    ranged = "[00:00:01-00:00:05] The cat sat.\n[00:00:05 - 00:00:09] It is here."
    assert _quality(point, sample) == _quality(ranged, sample)
    assert _quality(ranged, sample)[0] == 1.0
    imperfect, evidence = _quality(ranged.replace("cat", "bat"), sample)
    assert imperfect < 1.0
    assert evidence["errors"] == 1


@pytest.mark.parametrize("code", [
    "asr_worker_failed", "asr_worker_timeout", "asr_extraction_failed",
    "benchmark_provider_failed", "benchmark_content_not_verified",
    "tool_backend_exit", "tool_timeout", "tool_upstream_failed",
    "tool_permission_required", "tool_execution_failed", "tool_challenge_page",
])
def test_downstream_harness_network_and_gate_failures_cannot_demote(catalog, inputs, tmp_path, code):
    def failed(candidate, sample, options):
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=candidate, error_code=code, metadata={"tool_run": {"tool_version": "1.0", "tool_version_verified": True}})

    rows = [run_benchmark(**inputs, executor=failed)["rows"][0] for _ in range(2)]
    overlay = tmp_path / "overlay.jsonl"
    with pytest.raises(ValueError, match="not_tool_failure"):
        demote("primary.content", source="web_article", evidence_ids=[r["trial_id"] for r in rows], ledger_path=inputs["ledger_path"], overlay_path=overlay)
    assert not overlay.exists()


def test_quality_reproduces_from_sanitized_retained_text(catalog, inputs, tmp_path):
    private_text = "The source paragraph remains intact.\nhttps://example.org/source?token=private-trial-value\n"
    reference = tmp_path / "reference.txt"
    reference.write_text(private_text, encoding="utf-8")
    sample_path = Path(inputs["samples_path"])
    data = json.loads(sample_path.read_text(encoding="utf-8"))
    data["samples"][0].update(reference="reference.txt", metric="paragraph_recall")
    sample_path.write_text(json.dumps(data), encoding="utf-8")

    def extracted(candidate, sample, options):
        return replace(_result(candidate, sample, options), text=private_text)

    row = run_benchmark(**inputs, executor=extracted)["rows"][0]
    artifact = next(p for p in row["artifacts"] if Path(p["path"]).name == "content.md")
    retained = Path(artifact["path"]).read_text(encoding="utf-8")
    measured, evidence = _quality(retained, load_samples(sample_path)[0])
    assert row["quality"] == measured < 1.0
    assert row["quality_evidence"] == evidence
    assert row["chars"] == len(retained)
    assert "private-trial-value" not in retained
    assert "private-trial-value" not in Path(inputs["ledger_path"]).read_text(encoding="utf-8")


def test_selecting_sample_id_runs_the_requested_sample(catalog, inputs):
    path = Path(inputs["samples_path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["samples"].append({**data["samples"][0], "id": "second", "url": "https://example.org/second"})
    path.write_text(json.dumps(data), encoding="utf-8")
    observed = []

    def extracted(candidate, sample, options):
        observed.append((sample["id"], sample["url"]))
        return _result(candidate, sample, options)

    report = run_benchmark(**inputs, sample_id="second", executor=extracted)
    assert observed == [("second", "https://example.org/second")]
    assert report["sample_id"] == "second"
    assert report["rows"][0]["sample_id"] == "second"
