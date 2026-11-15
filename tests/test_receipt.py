from __future__ import annotations

import json
import tempfile
from pathlib import Path

from openmd.core.receipt import CompactReceiptRun


def test_t03_receipt_keeps_terminal_states_mutually_exclusive_and_skips_orthogonal() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        run = CompactReceiptRun.create(tmp_dir, "batch")
        statuses = ["verified"] * 6 + ["partial", "terminal_gap", "retryable_failure", "unattempted"]
        for index, status in enumerate(statuses):
            run.record_item(
                stable_identity=f"id-{index}",
                input_occurrence=1,
                status=status,
                skipped=index < 2,
                source_level="native" if status == "verified" else "partial_extraction",
                quality_status="passed" if status == "verified" else "not_evaluated",
            )
        payload, exit_code = run.finalize(discovery_complete=True)
        assert exit_code == 1
        assert payload["status"] == "partial"
        assert payload["counts"]["discovered"] == 10
        assert payload["counts"]["skipped"] == 2
        assert sum(payload["counts"][key] for key in CompactReceiptRun.TERMINAL_STATUSES) == 10
        summary = json.loads((Path(tmp_dir) / payload["summary_ref"]).read_text(encoding="utf-8"))
        assert summary["accounting_complete"] is True
        assert summary["execution_complete"] is False
        assert summary["coverage"] is None
        assert summary["cost"] == {"external_calls": None, "metered_amount": None}


def test_t02_receipt_is_bounded_and_does_not_persist_raw_urls_or_body() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 20 x 3 CJK chars = 180 UTF-8 bytes: under the 255-byte Linux/macOS component limit.
        run = CompactReceiptRun.create(Path(tmp_dir) / ("长目录" * 20), "ingest")
        error_codes = (
            "runtime_transient",
            "source_unavailable",
            "user_resolvable",
            "code_resolvable",
            "blocked",
            "package_validation_failed",
            "unknown",
        )
        for index in range(10_000):
            run.record_item(
                stable_identity=f"item-{index}",
                input_occurrence=1,
                status="failed",
                error_code=error_codes[index % len(error_codes)],
                error_summary="https://example.invalid/path?token=secret body must not persist",
            )
        payload, exit_code = run.finalize(discovery_complete=True)
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        assert exit_code == 1
        assert len(encoded) <= 2048
        assert len(payload["error_counts"]) == 5
        assert sum(payload["error_counts"].values()) + payload["other_error_count"] == 10_000
        content = (run.out_dir / payload["summary_ref"]).read_text(encoding="utf-8") + run.items_path.read_text(encoding="utf-8")
        assert "token=secret" not in content
        assert "body must not persist" not in content


def test_t08_incomplete_jsonl_is_only_tolerated_as_final_line() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        run = CompactReceiptRun.create(tmp_dir, "batch")
        run.record_item(stable_identity="one", input_occurrence=1, status="verified")
        with run.items_path.open("ab") as handle:
            handle.write(b'{"stable_identity":"partial"')
        rows, warnings = run.read_items_for_resume()
        assert len(rows) == 1
        assert warnings == ["truncated_final_jsonl_line"]

        run.items_path.write_text('{"stable_identity":"bad"\n{"stable_identity":"two"}\n', encoding="utf-8")
        try:
            run.read_items_for_resume()
        except ValueError as exc:
            assert "malformed_jsonl_line" in str(exc)
        else:
            raise AssertionError("middle corruption must fail")
