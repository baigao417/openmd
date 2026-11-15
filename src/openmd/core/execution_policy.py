"""Execution identity and compact package writer ownership rules."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path


FINGERPRINT_CONTRACT_VERSION = "openmd.low-token.v1"
COMPACT_LOCK_VERSION = "openmd.compact-lock.v2"


def execution_fingerprint(
    *,
    requested_profile: str | None,
    requested_modalities: tuple[str, ...],
    provider_chain: tuple[str, ...],
    backend_override: str | None,
    model: str | None,
    language: str | None,
    authorization_flags: dict[str, bool] | None = None,
    temporary_url: str | None = None,
    extraction_contract: dict[str, object] | None = None,
    effective_config: dict[str, object] | None = None,
) -> str:
    """Return a stable fingerprint without credentials, gates, or transient locators."""
    del authorization_flags, temporary_url
    payload = {
        "contract_version": FINGERPRINT_CONTRACT_VERSION,
        "requested_profile": requested_profile,
        "requested_modalities": list(requested_modalities),
        "provider_chain": list(provider_chain),
        "backend_override": backend_override,
        "model": model,
        "language": language,
        "extraction_contract": extraction_contract,
        "effective_config": effective_config,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_matching_fingerprint(metadata: dict[str, object], fingerprint: str) -> str | None:
    stored = metadata.get("execution_fingerprint")
    if not isinstance(stored, str) or not stored:
        return "execution_fingerprint_missing"
    if stored != fingerprint:
        return "execution_fingerprint_mismatch"
    return None


@dataclass
class CompactOutputLock:
    path: Path
    run_id: str
    _owner_token: str = field(default="", repr=False)
    _guard_descriptor: int | None = field(default=None, repr=False)

    @classmethod
    def acquire(cls, out_dir: str | Path, run_id: str) -> "CompactOutputLock":
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128 or not run_id.isascii():
            raise ValueError("compact lock requires a bounded ASCII run id")
        path = Path(out_dir) / ".openmd-compact.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never unlink the guard: replacing its inode would let two writers lock
        # different files. The OS releases this descriptor's lock after a crash.
        descriptor = os.open(path.parent / ".openmd-compact.guard", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire_guard(descriptor, path)
            existing = _read_owner(path)
            token = uuid.uuid4().hex
            owner = {"schema_version": COMPACT_LOCK_VERSION, "run_id": run_id, "owner_token": token}
            _install_owner(path, owner, replace_existing=existing is not None)
            return cls(path=path, run_id=run_id, _owner_token=token, _guard_descriptor=descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._guard_descriptor
        if descriptor is None:
            return
        try:
            owner = _read_owner(self.path)
            if owner and owner["run_id"] == self.run_id and owner["owner_token"] == self._owner_token:
                self.path.unlink()
        except OSError:
            # Preserve missing/changed/unknown ownership records. Closing the
            # guard is still mandatory, including a failed normal cleanup.
            pass
        finally:
            self._guard_descriptor = None
            os.close(descriptor)


def _acquire_guard(descriptor: int, owner_path: Path) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise FileExistsError(errno.EEXIST, "compact output has a live owner", str(owner_path)) from None
        raise


def _read_owner(path: Path) -> dict[str, str] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    try:
        owner = json.loads(raw) if len(raw) <= 4096 else None
    except (ValueError, UnicodeError):
        owner = None
    if (
        not isinstance(owner, dict)
        or set(owner) != {"schema_version", "run_id", "owner_token"}
        or owner.get("schema_version") != COMPACT_LOCK_VERSION
        or not isinstance(owner.get("run_id"), str)
        or not 0 < len(owner["run_id"]) <= 128
        or not owner["run_id"].isascii()
        or not isinstance(owner.get("owner_token"), str)
        or len(owner["owner_token"]) != 32
        or any(char not in "0123456789abcdef" for char in owner["owner_token"])
    ):
        # Legacy UUID-only locks did not hold this guard, so acquiring it cannot
        # establish that their owner is dead. Unknown/torn records also stay put.
        raise FileExistsError(errno.EEXIST, "compact output has an unknown ownership record", str(path))
    return owner


def _install_owner(path: Path, owner: dict[str, str], *, replace_existing: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".openmd-compact-owner-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(owner, separators=(",", ":")).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            # The guard proves this validated v2 owner is abandoned. Keep the
            # canonical path present while replacing it so legacy O_EXCL users
            # cannot slip into a remove/create gap.
            os.replace(temporary, path)
        else:
            # Atomically publish a complete record without overwriting an old
            # writer that may have created its legacy lock since our read.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
