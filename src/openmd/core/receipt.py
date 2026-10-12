"""Bounded, redacted receipts for opt-in compact CLI runs."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4


_MAX_COUNT = (1 << 63) - 1
_ERROR_CODES = frozenset(
    {
        "unknown",
        "runtime_transient",
        "source_unavailable",
        "user_resolvable",
        "code_resolvable",
        "blocked",
        "package_validation_failed",
    }
)


class CompactReceiptRun:
    TERMINAL_STATUSES = (
        "verified",
        "partial",
        "terminal_gap",
        "retryable_failure",
        "blocked",
        "failed",
        "unattempted",
    )

    def __init__(self, out_dir: Path, run_dir: Path, run_id: str, command: str) -> None:
        self.out_dir = out_dir
        self.run_dir = run_dir
        self.run_id = run_id
        self.command = command
        self.items_path = run_dir / "items.jsonl"
        self.summary_path = run_dir / "summary.json"
        self.checkpoint_path = run_dir / "checkpoint.json"
        self._counts: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._seen: set[str] = set()
        self._occurrences = 0
        self.expected: dict[str, int] = {}
        self.requires_fresh_stdin = False

    @classmethod
    def create(cls, out_dir: str | Path, command: str) -> "CompactReceiptRun":
        root = Path(out_dir)
        run_id = uuid4().hex
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        run = cls(root, run_dir, run_id, command)
        run.items_path.touch(exist_ok=False)
        run.write_checkpoint()
        return run

    def has_item(self, identity: str) -> bool:
        return identity in self._seen

    @property
    def has_items(self) -> bool:
        return bool(self._seen)

    def replace_expected(self, identities: dict[str, int]) -> None:
        """Persist only redacted identities, never replayable input URLs."""
        self.expected = dict(identities)
        self._atomic_write_json(self.run_dir / "expected.json", self.expected)
        self.write_checkpoint()

    def add_occurrences(self, identity: str, occurrence: int) -> None:
        """Reconcile a newly resolved alias against a completed item in this run."""
        if identity not in self._seen:
            return
        rows, warnings = self.read_items_for_resume()
        if warnings:
            raise ValueError("cannot amend interrupted receipt")
        for row in rows:
            if row["stable_identity"] == identity:
                row["input_occurrence"] += occurrence
                break
        self._atomic_write_text(
            self.items_path,
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        )
        self._occurrences += occurrence
        self.write_checkpoint()

    def write_checkpoint(self) -> None:
        self._atomic_write_json(self.checkpoint_path, {
            "schema_version": "openmd.compact-checkpoint.v1",
            "run_id": self.run_id,
            "completed": len(self._seen),
            "expected_ref": "expected.json" if self.expected else None,
            "expected_count": len(self.expected),
            "requires_fresh_stdin": self.requires_fresh_stdin,
        })

    def record_item(
        self,
        *,
        stable_identity: str,
        input_occurrence: int,
        status: str,
        skipped: bool = False,
        source_level: str | None = None,
        quality_status: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        failure_class: str | None = None,
        recovery_type: str | None = None,
        artifact_ref: str | None = None,
    ) -> None:
        if status not in self.TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        if not stable_identity or stable_identity in self._seen:
            raise ValueError("stable identity must be unique and non-empty")
        if type(input_occurrence) is not int or input_occurrence < 1 or input_occurrence > _MAX_COUNT:
            raise ValueError("input occurrence must be a positive integer")
        normalized_error = self._normalize_error_code(error_code)
        record = {
            "stable_identity": stable_identity,
            "input_occurrence": input_occurrence,
            "status": status,
            "skipped": skipped,
            "artifact_ref": artifact_ref,
            "source_level": source_level,
            "quality_status": quality_status or "not_evaluated",
            "error_code": normalized_error,
            "failure_class": failure_class,
            "recovery_type": recovery_type,
        }
        # Error text may contain signed URLs, headers, or source content; never persist it.
        del error_summary
        with self.items_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._seen.add(stable_identity)
        self._occurrences += input_occurrence
        self._counts[status] += 1
        if skipped:
            self._counts["skipped"] += 1
        if normalized_error:
            self._errors[normalized_error] += 1
        self.write_checkpoint()

    def finalize(
        self,
        *,
        discovery_complete: bool,
        manifest_only: bool = False,
        resume_supported: bool = False,
        requires_fresh_stdin: bool = False,
        limitations: list[str] | None = None,
        failed: bool = False,
    ) -> tuple[dict[str, Any], int]:
        counts = {status: self._bounded_count(self._counts[status]) for status in self.TERMINAL_STATUSES}
        counts["discovered"] = self._bounded_count(sum(counts.values()))
        counts["skipped"] = self._bounded_count(self._counts["skipped"])
        counts["input_occurrences"] = self._bounded_count(self._occurrences)
        counts["duplicate_inputs"] = self._bounded_count(max(0, self._occurrences - counts["discovered"]))
        accounting_complete = not self.expected or (
            set(self.expected) == self._seen and sum(self.expected.values()) == self._occurrences
        )
        execution_complete = accounting_complete and counts["unattempted"] == 0
        status = self._status_for(counts, discovery_complete=discovery_complete, manifest_only=manifest_only)
        if failed or not accounting_complete:
            status = "failed"
        accessible = counts["discovered"] if discovery_complete and not any(
            counts[state] for state in ("terminal_gap", "retryable_failure", "blocked", "failed", "unattempted")
        ) else None
        coverage = counts["verified"] / accessible if accessible else None
        all_limitations = list(limitations or [])
        if counts["discovered"] == 0 and "empty_scope" not in all_limitations:
            all_limitations.append("empty_scope")
        if requires_fresh_stdin and "requires_fresh_stdin" not in all_limitations:
            all_limitations.append("requires_fresh_stdin")
        summary = {
            "schema_version": "openmd.run-summary.v1",
            "run_id": self.run_id,
            "command": self.command,
            "status": status,
            "counts": counts,
            "discovery_complete": discovery_complete,
            "accounting_complete": accounting_complete,
            "execution_complete": execution_complete,
            "coverage": coverage,
            "accessible": accessible,
            "resume_supported": resume_supported,
            "items_ref": "items.jsonl",
            "checkpoint_ref": "checkpoint.json",
            "cost": {"external_calls": None, "metered_amount": None},
            "limitations": all_limitations,
        }
        self.requires_fresh_stdin = requires_fresh_stdin
        self.write_checkpoint()
        self._atomic_write_json(self.summary_path, summary)
        top_errors = sorted(self._errors.items(), key=lambda item: (-item[1], item[0]))
        selected = dict(top_errors[:5])
        other = sum(count for _, count in top_errors[5:])
        summary_ref = self.summary_path.relative_to(self.out_dir).as_posix()
        payload = {
            "schema_version": "openmd.receipt.v1",
            "status": status,
            "counts": counts,
            "summary_ref": summary_ref,
            "error_counts": selected,
            "other_error_count": other,
        }
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(rendered.encode("utf-8")) > 2048:
            raise ValueError("compact receipt exceeds 2048 UTF-8 bytes")
        return payload, 0 if status in {"complete", "discovery_only"} else 1

    def read_items_for_resume(self) -> tuple[list[dict[str, Any]], list[str]]:
        raw = self.items_path.read_bytes()
        lines = raw.splitlines(keepends=True)
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, line in enumerate(lines):
            final_line = index == len(lines) - 1
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if final_line and not line.endswith((b"\n", b"\r")):
                    warnings.append("truncated_final_jsonl_line")
                    break
                raise ValueError("malformed_jsonl_line") from exc
        return rows, warnings

    @staticmethod
    def stable_identity(*parts: str | None) -> str:
        material = "\x1f".join(part or "" for part in parts)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _normalize_error_code(value: str | None) -> str | None:
        if not value:
            return None
        normalized = str(value).lower()
        return normalized if normalized in _ERROR_CODES else "unknown"

    @staticmethod
    def _bounded_count(value: int) -> int:
        if type(value) is not int or value < 0 or value > _MAX_COUNT:
            raise ValueError("receipt count is outside signed 64-bit range")
        return value

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: dict[str, Any]) -> None:
        cls._atomic_write_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @classmethod
    def _status_for(cls, counts: dict[str, int], *, discovery_complete: bool, manifest_only: bool) -> str:
        if counts["discovered"] == 0:
            return "blocked"
        if manifest_only and discovery_complete:
            return "discovery_only"
        if counts["failed"] or counts["retryable_failure"]:
            return "partial" if counts["verified"] or counts["partial"] else "failed"
        if counts["blocked"] and not (counts["verified"] or counts["partial"]):
            return "blocked"
        if not discovery_complete or counts["partial"] or counts["terminal_gap"] or counts["unattempted"] or counts["blocked"]:
            return "partial"
        return "complete"
