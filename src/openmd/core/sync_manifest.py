"""Atomic, locked synchronization for sources declared to grow over time."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "openmd.sync-manifest/v1"
OPERATIONS = frozenset({"created", "updated", "unchanged", "conflict_kept_user_edit", "restored_missing"})


def is_syncable(item, *, test_source: str | None = None) -> bool:
    """The test override is explicit and scoped to a single platform, never global."""
    from importlib.resources import files
    from openmd.routing.loader import load_catalog

    test_source = test_source or os.getenv("OPENMD_TEST_SYNCABLE_SOURCE")
    if test_source and item.platform == test_source:
        return True
    catalog = load_catalog()
    if any(record.get("route_source") == item.platform and record.get("syncable") is True for record in catalog.capability_metadata):
        return True
    traits = json.loads(files("openmd.routing").joinpath("routes.json").read_text(encoding="utf-8")).get("source_traits", {})
    return traits.get(item.platform, {}).get("syncable") is True


def source_id_for(item) -> str:
    # A resolved platform ID wins; the fallback hashes the sanitized URL so
    # credentials and signed query strings never become manifest identifiers.
    from .secrets import sanitize_url

    identifier = str(item.id).strip() if item.id else hashlib.sha256(sanitize_url(item.url).encode("utf-8")).hexdigest()
    return f"{item.platform}:{identifier}"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path, timeout: float = 30):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        acquired = False
        deadline = time.monotonic() + timeout
        try:
            while not acquired:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("sync_manifest_lock_timeout")
                    time.sleep(0.02)
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class SyncWriteResult:
    path: Path
    operation: str


class SyncManifest:
    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir).resolve()
        self.path = self.root / ".openmd" / "manifest.json"

    def _target(self, relative: str) -> Path:
        path = Path(relative)
        target = (self.root / path).resolve()
        if path.is_absolute() or not target.is_relative_to(self.root) or target == self.root or target.is_relative_to(self.path.parent.resolve()):
            raise ValueError("sync_path_outside_output")
        return target

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "entries": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("entries"), dict):
                raise ValueError("sync_manifest_invalid")
            for key, entry in data["entries"].items():
                if not isinstance(entry, dict) or entry.get("source_id") != key or entry.get("schema") != SCHEMA:
                    raise ValueError("sync_manifest_entry_invalid")
                self._target(entry["path"])
                digest = entry["content_sha256"]
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ValueError("sync_manifest_hash_invalid")
            return data
        except (ValueError, KeyError, TypeError, UnicodeError):
            backup = self.path.with_name(f"manifest.corrupt-{uuid.uuid4().hex}.json")
            os.replace(self.path, backup)
            return {"schema": SCHEMA, "entries": {}}

    def _free_target(self, target: Path, content: bytes | None = None) -> Path:
        candidate = target
        counter = 2
        while candidate.exists():
            if content is not None and candidate.is_file() and candidate.read_bytes() == content:
                return candidate
            candidate = target.with_name(f"{target.stem}-{counter}{target.suffix}")
            counter += 1
        return candidate

    def write(self, source_id: str, relative_path: str, content: str, *, cursor=None) -> SyncWriteResult:
        proposed = self._target(relative_path)
        encoded = content.encode("utf-8")
        digest = _sha(encoded)
        with _file_lock(self.path.with_suffix(".lock")):
            data = self._load()
            entry = data["entries"].get(source_id)
            target = self._target(entry["path"]) if entry else self._free_target(proposed)
            operation = "created"
            if entry:
                if not target.exists():
                    operation = "restored_missing"
                elif not target.is_file() or _sha(target.read_bytes()) != entry["content_sha256"]:
                    update = self._free_target(target.with_name(f"{target.stem}.openmd-update.md"), encoded)
                    if not update.exists():
                        _atomic_write(update, encoded)
                    # Retain the canonical path and last canonical hash: future
                    # runs must continue to protect the user's edited original.
                    return SyncWriteResult(update, "conflict_kept_user_edit")
                elif digest == entry["content_sha256"]:
                    operation = "unchanged"
                else:
                    operation = "updated"
            if operation != "unchanged":
                _atomic_write(target, encoded)
            data["entries"][source_id] = {
                "source_id": source_id, "path": target.relative_to(self.root).as_posix(),
                "content_sha256": digest, "cursor": cursor,
                "last_synced": datetime.now(timezone.utc).isoformat(), "schema": SCHEMA,
            }
            _atomic_write(self.path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return SyncWriteResult(target, operation)
