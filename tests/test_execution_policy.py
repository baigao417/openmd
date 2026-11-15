from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.douyin_providers import DOUYIN_ASR_BACKEND, DouyinTranscriptProvider
from openmd.backends.registry import BackendRuntimeOptions
import openmd.core.execution_policy as policy
from openmd.core.execution_policy import CompactOutputLock, execution_fingerprint, require_matching_fingerprint
from openmd.core.models import ContentItem
from openmd.core.package_writer import write_item_package
from openmd.core.models import TranscriptResult


class RecordingAsr:
    name = DOUYIN_ASR_BACKEND

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path: Path, *, timeout: int) -> str:
        self.calls += 1
        assert audio_path.exists()
        return "fake cloud transcript"


def _item(**raw_meta: object) -> ContentItem:
    return ContentItem(
        platform="douyin",
        input_type="single",
        url="https://www.douyin.com/video/fixture",
        id="fixture",
        title="fixture",
        author="fixture",
        source_type="video",
        raw_meta=raw_meta,
    )


def test_cloud_gate_blocks_before_media_preparation_but_not_native_text() -> None:
    asr = RecordingAsr()
    runner_calls: list[list[str]] = []
    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_media_download_backends=True, enable_asr_backends=True),
        runner=lambda command, timeout: (runner_calls.append(command) or (0, "", "")),
        asr_client=asr,
    )
    native = provider.extract(BackendExtractRequest(item=_item(transcript_candidates=[{"text": "native"}]), backend_chain=(provider.name,), allow_fallback=False))
    assert native.has_usable_text
    blocked = provider.extract(BackendExtractRequest(item=_item(play_url="https://media.example/video"), backend_chain=(provider.name,), allow_fallback=False))
    assert blocked.error_code == "douyin_cloud_backends_not_enabled"
    assert runner_calls == []
    assert asr.calls == 0


def test_explicit_cloud_gate_allows_fake_asr_without_real_network() -> None:
    asr = RecordingAsr()

    def runner(command: list[str], timeout: int) -> tuple[int, str, str]:
        Path(command[-1]).write_bytes(b"audio")
        return 0, "", ""

    provider = DouyinTranscriptProvider(
        BackendRuntimeOptions(enable_media_download_backends=True, enable_asr_backends=True, enable_cloud_backends=True),
        runner=runner,
        asr_client=asr,
    )
    result = provider.extract(BackendExtractRequest(item=_item(play_url="https://media.example/video"), backend_chain=(provider.name,), allow_fallback=False))
    assert result.has_usable_text
    assert asr.calls == 1


def test_fingerprint_is_stable_and_does_not_include_authorization_or_temporary_values() -> None:
    first = execution_fingerprint(
        requested_profile="media",
        requested_modalities=("transcript",),
        provider_chain=("douyin.transcript",),
        backend_override=None,
        model="sensevoice",
        language="zh",
        authorization_flags={"enable_cloud_backends": True},
        temporary_url="https://media.example/video?token=secret",
    )
    second = execution_fingerprint(
        requested_profile="media",
        requested_modalities=("transcript",),
        provider_chain=("douyin.transcript",),
        backend_override=None,
        model="sensevoice",
        language="zh",
        authorization_flags={"enable_cloud_backends": False},
        temporary_url="https://other.example/video?token=other",
    )
    assert first == second
    assert "secret" not in first
    assert require_matching_fingerprint({"execution_fingerprint": first}, first) is None
    assert require_matching_fingerprint({}, first) == "execution_fingerprint_missing"
    assert require_matching_fingerprint({"execution_fingerprint": "old"}, first) == "execution_fingerprint_mismatch"


def test_compact_output_lock_is_exclusive_and_does_not_remove_residual_lock(tmp_path: Path) -> None:
    first = CompactOutputLock.acquire(tmp_path, "a" * 32)
    with pytest.raises(FileExistsError):
        CompactOutputLock.acquire(tmp_path, "b" * 32)
    first.release()
    lock_path = tmp_path / ".openmd-compact.lock"
    lock_path.write_text("residual", encoding="utf-8")
    with pytest.raises(FileExistsError):
        CompactOutputLock.acquire(tmp_path, "c" * 32)
    assert lock_path.read_text(encoding="utf-8") == "residual"


def test_lock_release_preserves_guard_identity_and_stale_release_cannot_remove_new_owner(tmp_path: Path) -> None:
    first = CompactOutputLock.acquire(tmp_path, "first")
    guard = tmp_path / ".openmd-compact.guard"
    guard_identity = guard.stat().st_ino
    first.release()
    assert not first.path.exists()
    assert guard.exists()
    second = CompactOutputLock.acquire(tmp_path, "second")
    try:
        second_record = second.path.read_bytes()
        first.release()
        assert second.path.read_bytes() == second_record
        assert guard.stat().st_ino == guard_identity
    finally:
        second.release()


@pytest.mark.parametrize("record", [
    b"a" * 32, b"", b"\xff", b'{"schema_version":',
    json.dumps({"schema_version": "unknown", "run_id": "x", "owner_token": "a" * 32}).encode(),
    json.dumps({"schema_version": policy.COMPACT_LOCK_VERSION, "run_id": "x", "owner_token": "bad"}).encode(),
])
def test_unknown_or_torn_owner_is_preserved_and_guard_is_not_leaked(tmp_path: Path, record: bytes) -> None:
    path = tmp_path / ".openmd-compact.lock"
    path.write_bytes(record)
    with pytest.raises(FileExistsError):
        CompactOutputLock.acquire(tmp_path, "new-owner")
    assert path.read_bytes() == record
    # Remove only this test's deliberately malformed fixture and prove that a
    # failed acquisition did not leave a live advisory lock in this process.
    path.unlink()
    lock = CompactOutputLock.acquire(tmp_path, "new-owner")
    lock.release()


def test_legacy_writer_racing_first_install_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    original_link = os.link

    def race(source, destination):
        Path(destination).write_text("legacy-live-owner", encoding="ascii")
        original_link(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(policy.os, "link", race)
        with pytest.raises(FileExistsError):
            CompactOutputLock.acquire(tmp_path, "new-owner")
    assert (tmp_path / ".openmd-compact.lock").read_text() == "legacy-live-owner"
    assert not list(tmp_path.glob(".openmd-compact-owner-*.tmp"))


def test_failed_owner_fsync_publishes_no_partial_record_and_releases_guard(tmp_path: Path, monkeypatch) -> None:
    def fail_fsync(descriptor):
        raise OSError("fixture disk write failure")

    with monkeypatch.context() as patch:
        patch.setattr(policy.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="disk write failure"):
            CompactOutputLock.acquire(tmp_path, "first")
    assert not (tmp_path / ".openmd-compact.lock").exists()
    assert not list(tmp_path.glob(".openmd-compact-owner-*.tmp"))
    lock = CompactOutputLock.acquire(tmp_path, "second")
    lock.release()


def test_package_writer_persists_execution_fingerprint_without_policy_inputs(tmp_path: Path) -> None:
    item = ContentItem(
        platform="web_article",
        input_type="single",
        url="https://example.com/article",
        id="article",
        title="article",
        author=None,
        source_type="article",
        raw_meta={},
    )
    result = TranscriptResult(text="body", status="verified", source_level="native", backend="mock")
    fingerprint = execution_fingerprint(
        requested_profile="article",
        requested_modalities=("body",),
        provider_chain=("mock",),
        backend_override="mock",
        model=None,
        language="unknown",
    )
    written = write_item_package(item, result, tmp_path, execution_fingerprint=fingerprint)
    metadata = __import__("json").loads((written.package_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["execution_fingerprint"] == fingerprint
