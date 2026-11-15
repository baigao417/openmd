from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import openmd.cli as cli_module
import pytest
from openmd.backends.base import DiscoveryResult
from openmd.cli import CliResult
from openmd.core.models import ContentItem
from openmd.core.receipt import CompactReceiptRun
from openmd.core.runlog import append_runlog_entry, build_runlog_entry
from openmd.cli import main as cli_main


def test_compact_ingest_mock_writes_a_single_bounded_receipt_and_readable_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(
                [
                    "ingest",
                    "https://example.com/article?token=secret",
                    "--out",
                    tmp_dir,
                    "--backend",
                    "mock",
                    "--receipt",
                    "compact",
                ]
            )
        lines = stdout.getvalue().splitlines()
        assert exit_code == 0
        assert len(lines) == 1
        assert len((lines[0] + "\n").encode("utf-8")) <= 2048
        receipt = json.loads(lines[0])
        assert set(receipt) == {
            "schema_version",
            "status",
            "counts",
            "summary_ref",
            "error_counts",
            "other_error_count",
        }
        summary = json.loads((Path(tmp_dir) / receipt["summary_ref"]).read_text(encoding="utf-8"))
        run_dir = Path(tmp_dir) / receipt["summary_ref"]
        rows = [json.loads(line) for line in (run_dir.parent / summary["items_ref"]).read_text(encoding="utf-8").splitlines()]
        assert receipt["status"] == "complete"
        assert summary["counts"]["discovered"] == 1
        assert len(rows) == 1
        assert "token=secret" not in json.dumps({"summary": summary, "rows": rows})


@pytest.mark.parametrize("count", (1, 100, 1_000, 10_000))
def test_t01_compact_cli_mock_scales_without_expanding_stdout(monkeypatch, count: int) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest = Path(tmp_dir) / "inputs.txt"
        manifest.write_text("\n".join(f"https://example.com/article-{index}" for index in range(count)), encoding="utf-8")
        calls: list[str] = []

        def fake_process(url: str, args, *, command: str) -> CliResult:
            calls.append(url)
            append_runlog_entry(
                cli_module._runlog_path(args),
                build_runlog_entry(
                    input_url=url,
                    platform="web_article",
                    backend="fixture",
                    status="verified",
                    source_type="article",
                    source_level="native_text",
                ),
            )
            return CliResult(ok=True, command=command, item_count=1, success_count=1)

        monkeypatch.setattr(cli_module, "_process_input", fake_process)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(["batch", str(manifest), "--out", tmp_dir, "--receipt", "compact"])

        lines = stdout.getvalue().splitlines()
        receipt = json.loads(lines[0])
        summary_path = Path(tmp_dir) / receipt["summary_ref"]
        item_rows = (summary_path.parent / "items.jsonl").read_text(encoding="utf-8").splitlines()
        assert exit_code == 0
        assert len(lines) == 1
        assert len((lines[0] + "\n").encode("utf-8")) <= 2048
        assert len(calls) == count
        assert len(item_rows) == count
        assert receipt["counts"]["discovered"] == count


def test_t11_bad_or_mismatched_compact_package_blocks_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        args = [
            "ingest",
            "https://example.com/article",
            "--out",
            tmp_dir,
            "--backend",
            "mock",
            "--package",
            "--receipt",
            "compact",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli_main(args) == 0
        package_path = next((Path(tmp_dir) / "packages").rglob("*.openmd"))
        metadata_path = package_path / "metadata.json"

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["execution_fingerprint"] = "different-fingerprint"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        mismatch_before = metadata_path.read_bytes()
        mismatch_stdout = io.StringIO()
        with contextlib.redirect_stdout(mismatch_stdout):
            assert cli_main(args) == 1
        assert json.loads(mismatch_stdout.getvalue())["status"] == "blocked"
        assert metadata_path.read_bytes() == mismatch_before

        metadata_path.write_text("{not valid json", encoding="utf-8")
        corrupt_before = metadata_path.read_bytes()
        corrupt_stdout = io.StringIO()
        with contextlib.redirect_stdout(corrupt_stdout):
            assert cli_main(args) == 1
        assert json.loads(corrupt_stdout.getvalue())["status"] == "blocked"
        assert metadata_path.read_bytes() == corrupt_before


def test_compact_package_residual_lock_blocks_before_extraction_and_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        lock = Path(tmp_dir) / ".openmd-compact.lock"
        lock.write_text("residual", encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(
                [
                    "ingest",
                    "https://example.com/article",
                    "--out",
                    tmp_dir,
                    "--backend",
                    "mock",
                    "--package",
                    "--receipt",
                    "compact",
                ]
            )
        receipt = json.loads(stdout.getvalue())
        assert exit_code == 1
        assert receipt["status"] == "blocked"
        assert lock.read_text(encoding="utf-8") == "residual"
        assert not (Path(tmp_dir) / "packages").exists()


def test_compact_package_only_skips_when_execution_fingerprint_matches() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        args = [
            "ingest",
            "https://example.com/article",
            "--out",
            tmp_dir,
            "--backend",
            "mock",
            "--package",
            "--receipt",
            "compact",
        ]
        first_out = io.StringIO()
        with contextlib.redirect_stdout(first_out):
            assert cli_main(args) == 0
        second_out = io.StringIO()
        with contextlib.redirect_stdout(second_out):
            assert cli_main(args) == 0
        second = json.loads(second_out.getvalue())
        assert second["counts"]["skipped"] == 1

        package_path = next((Path(tmp_dir) / "packages").rglob("*.openmd"))
        metadata_path = package_path / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert "execution_fingerprint" in metadata
        metadata.pop("execution_fingerprint")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        before = metadata_path.read_bytes()

        blocked_out = io.StringIO()
        with contextlib.redirect_stdout(blocked_out):
            assert cli_main(args) == 1
        blocked = json.loads(blocked_out.getvalue())
        assert blocked["status"] == "blocked"
        assert metadata_path.read_bytes() == before


def test_compact_receipt_rejects_package_mutation_options_before_execution() -> None:
    for option in ("--update", "--overwrite", "--new-version"):
        with tempfile.TemporaryDirectory() as tmp_dir:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = cli_main(
                    [
                        "ingest",
                        "https://example.com/article",
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--package",
                        "--receipt",
                        "compact",
                        option,
                    ]
                )
            assert exit_code == 1
            assert "--receipt compact cannot be combined" in stderr.getvalue()
            assert not (Path(tmp_dir) / "packages").exists()


def test_t04_compact_batch_dedupes_identical_inputs_before_extraction_and_tracks_occurrences(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest = Path(tmp_dir) / "urls.txt"
        first_signed_url = "https://example.com/article?signature=first-secret"
        second_signed_url = "https://example.com/article?signature=second-secret"
        manifest.write_text(f"{first_signed_url}\n{second_signed_url}\n", encoding="utf-8")
        calls: list[str] = []

        def fake_process(url: str, args, *, command: str) -> CliResult:
            calls.append(url)
            append_runlog_entry(
                cli_module._runlog_path(args),
                build_runlog_entry(
                    input_url=url,
                    platform="web_article",
                    backend="fixture",
                    status="verified",
                    source_type="article",
                    source_level="native_text",
                ),
            )
            return CliResult(ok=True, command=command, item_count=1, success_count=1)

        monkeypatch.setattr(cli_module, "_process_input", fake_process)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(
                [
                    "batch",
                    str(manifest),
                    "--out",
                    tmp_dir,
                    "--receipt",
                    "compact",
                ]
            )
        assert exit_code == 0
        assert calls == [first_signed_url]
        receipt = json.loads(stdout.getvalue())
        summary_path = Path(tmp_dir) / receipt["summary_ref"]
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["counts"]["discovered"] == 1
        assert summary["counts"]["input_occurrences"] == 2
        assert summary["counts"]["duplicate_inputs"] == 1
        assert summary["counts"]["skipped"] == 0
        assert "first-secret" not in (summary_path.parent / "items.jsonl").read_text(encoding="utf-8")
        assert "second-secret" not in (summary_path.parent / "items.jsonl").read_text(encoding="utf-8")


def test_t04_compact_batch_keeps_different_resource_ids_separate(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest = Path(tmp_dir) / "urls.txt"
        first_url = "https://example.com/article?id=first&signature=one"
        second_url = "https://example.com/article?id=second&signature=two"
        manifest.write_text(f"{first_url}\n{second_url}\n", encoding="utf-8")
        calls: list[str] = []

        def fake_process(url: str, args, *, command: str) -> CliResult:
            calls.append(url)
            append_runlog_entry(
                cli_module._runlog_path(args),
                build_runlog_entry(
                    input_url=url,
                    platform="web_article",
                    backend="fixture",
                    status="verified",
                    source_type="article",
                    source_level="native_text",
                ),
            )
            return CliResult(ok=True, command=command, item_count=1, success_count=1)

        monkeypatch.setattr(cli_module, "_process_input", fake_process)
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli_main(["batch", str(manifest), "--out", tmp_dir, "--receipt", "compact"]) == 0
        assert calls == [first_url, second_url]


def test_compact_batch_missing_input_writes_a_bounded_failure_receipt() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing = Path(tmp_dir) / "missing.txt"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(
                ["batch", str(missing), "--out", tmp_dir, "--receipt", "compact"]
            )
        assert exit_code == 1
        receipt = json.loads(stdout.getvalue())
        assert receipt["status"] == "blocked"
        assert receipt["summary_ref"] is not None
        assert (Path(tmp_dir) / receipt["summary_ref"]).is_file()


def test_t07_compact_summary_write_failure_emits_bounded_failure_without_mutating_prior_run(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        first_stdout = io.StringIO()
        with contextlib.redirect_stdout(first_stdout):
            assert cli_main(["ingest", "https://example.com/first", "--out", tmp_dir, "--backend", "mock", "--receipt", "compact"]) == 0
        first_summary = Path(tmp_dir) / json.loads(first_stdout.getvalue())["summary_ref"]
        before = first_summary.read_bytes()

        def fail_atomic_write(path: Path, payload: dict[str, object]) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr(CompactReceiptRun, "_atomic_write_json", staticmethod(fail_atomic_write))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli_main(["ingest", "https://example.com/second", "--out", tmp_dir, "--backend", "mock", "--receipt", "compact"])

        receipt = json.loads(stdout.getvalue())
        assert exit_code == 1
        assert receipt["status"] == "failed"
        assert receipt["summary_ref"] is None
        assert len((stdout.getvalue()).encode("utf-8")) <= 2048
        assert "disk full" not in stdout.getvalue()
        assert stderr.getvalue() == ""
        assert first_summary.read_bytes() == before


def test_t07_compact_run_directory_failure_still_emits_a_bounded_receipt(monkeypatch) -> None:
    def fail_create(cls, out_dir, command):
        raise OSError("simulated read-only output")

    monkeypatch.setattr(CompactReceiptRun, "create", classmethod(fail_create))
    with tempfile.TemporaryDirectory() as tmp_dir:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli_main(["ingest", "https://example.com/article", "--out", tmp_dir, "--backend", "mock", "--receipt", "compact"])

        receipt = json.loads(stdout.getvalue())
        assert exit_code == 1
        assert receipt["status"] == "failed"
        assert receipt["summary_ref"] is None
        assert len(stdout.getvalue().encode("utf-8")) <= 2048
        assert stderr.getvalue() == ""


def test_t12_signed_stdin_is_not_written_to_compact_receipt_artifacts(monkeypatch) -> None:
    signed_input = "https://example.com/article?signature=very-secret-value"
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setattr(sys, "stdin", io.StringIO(signed_input + "\n"))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli_main(["ingest", "-", "--out", tmp_dir, "--backend", "mock", "--receipt", "compact"])

        receipt = json.loads(stdout.getvalue())
        run_dir = Path(tmp_dir) / receipt["summary_ref"]
        compact_text = "".join(
            [
                stdout.getvalue(),
                stderr.getvalue(),
                (run_dir.parent / "summary.json").read_text(encoding="utf-8"),
                (run_dir.parent / "items.jsonl").read_text(encoding="utf-8"),
                (run_dir.parent / "checkpoint.json").read_text(encoding="utf-8"),
            ]
        )
        assert exit_code == 0
        assert "very-secret-value" not in compact_text
        assert json.loads((run_dir.parent / "checkpoint.json").read_text(encoding="utf-8"))["requires_fresh_stdin"] is True


def test_t05_incomplete_all_discovery_cannot_be_complete(monkeypatch) -> None:
    items = [
        ContentItem(
            platform="douyin",
            input_type="collection",
            url=f"https://www.douyin.com/video/fixture-{index}",
            id=f"fixture-{index}",
            title=f"fixture {index}",
            author="fixture",
            source_type="video",
            raw_meta={},
        )
        for index in range(2)
    ]

    class IncompleteDiscoveryExecutor:
        def discover(self, request):
            return DiscoveryResult.success(
                provider="fixture.discovery",
                items=items,
                metadata={"source_exhausted": False},
            )

    def fake_process(discovered_items, args, *, command: str) -> CliResult:
        for item in discovered_items:
            append_runlog_entry(
                cli_module._runlog_path(args),
                build_runlog_entry(
                    input_url=item.url,
                    platform=item.platform,
                    backend="fixture",
                    status="verified",
                    source_type=item.source_type,
                    source_level="native_text",
                ),
            )
        return CliResult(ok=True, command=command, item_count=len(discovered_items), success_count=len(discovered_items))

    monkeypatch.setattr(cli_module, "_build_discovery_executor", lambda *args, **kwargs: IncompleteDiscoveryExecutor())
    monkeypatch.setattr(cli_module, "_process_items", fake_process)
    with tempfile.TemporaryDirectory() as tmp_dir:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(
                [
                    "collect",
                    "https://www.douyin.com/user/fixture",
                    "--out",
                    tmp_dir,
                    "--all",
                    "--enable-browser-backends",
                    "--receipt",
                    "compact",
                ]
            )
        receipt = json.loads(stdout.getvalue())
        summary = json.loads((Path(tmp_dir) / receipt["summary_ref"]).read_text(encoding="utf-8"))
        assert exit_code == 1
        assert receipt["status"] == "partial"
        assert summary["discovery_complete"] is False
        assert summary["counts"]["discovered"] == 2


def test_t14_legacy_mode_keeps_its_existing_result_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_main(["ingest", "https://example.com/article", "--out", tmp_dir, "--backend", "mock"])

        payload = json.loads(stdout.getvalue())
        assert exit_code == 0
        assert payload["ok"] is True
        assert payload["command"] == "ingest"
        assert "summary_ref" not in payload


def test_compact_manifest_only_records_discovery_without_extraction() -> None:
    items = [
        ContentItem(
            platform="youtube",
            input_type="collection",
            url=f"https://www.youtube.com/watch?v=fixture-{index}",
            id=f"fixture-{index}",
            title=f"fixture {index}",
            author="fixture",
            source_type="video",
            raw_meta={},
        )
        for index in range(2)
    ]

    class DiscoveryOnlyExecutor:
        def discover(self, request):
            return DiscoveryResult.success(provider="fixture.discovery", items=items)

    old_discovery = cli_module._build_discovery_executor
    old_backend = cli_module._build_backend_executor
    try:
        cli_module._build_discovery_executor = lambda *args, **kwargs: DiscoveryOnlyExecutor()  # type: ignore[assignment]
        cli_module._build_backend_executor = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manifest-only must not create extraction executor"))  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        "https://www.youtube.com/@fixture/videos",
                        "--out",
                        tmp_dir,
                        "--manifest-only",
                        "--receipt",
                        "compact",
                    ]
                )
            receipt = json.loads(stdout.getvalue())
            summary = json.loads((Path(tmp_dir) / receipt["summary_ref"]).read_text(encoding="utf-8"))
            assert exit_code == 0
            assert receipt["status"] == "discovery_only"
            assert summary["counts"]["discovered"] == 2
            assert summary["counts"]["verified"] == 0
    finally:
        cli_module._build_discovery_executor = old_discovery  # type: ignore[assignment]
        cli_module._build_backend_executor = old_backend  # type: ignore[assignment]
