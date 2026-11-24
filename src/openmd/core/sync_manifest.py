"""Atomic, locked synchronization for sources declared to grow over time."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
    if item.platform == "ai_chat" and item.raw_meta.get("chat_site") in {"chatgpt", "claude"}:
        from openmd.backends.ai_chat import conversation_id_for_url

        site = item.raw_meta["chat_site"]
        return site + ":" + (conversation_id_for_url(site, item.url) or identifier)
    return f"{item.platform}:{identifier}"


def prepare_sync_item(item, output_dir):
    """Supply a provider's persisted cursor without changing explicit inputs."""
    if not item.raw_meta.get("sync_cursor_enabled") or not is_syncable(item):
        return item
    manifest = SyncManifest(output_dir)
    entry = manifest.read(source_id_for(item))
    if not entry or not manifest._target(entry["path"]).is_file():
        return item
    return replace(item, raw_meta={**item.raw_meta, "sync_cursor": entry.get("cursor"),
                                   "sync_source_id": entry["source_id"]})


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
        last_error: PermissionError | None = None
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt == 4:
                    raise
                # Windows Defender/indexers and antivirus can briefly hold the
                # destination or staging file. Keep the retry bounded and
                # surface the original error if the handle does not clear.
                time.sleep(0.02 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        temporary.unlink(missing_ok=True)


def _open_lock_file(path: Path, deadline: float):
    # Windows can briefly refuse to open the lock file while another writer holds it
    # (sharing violation); treat that like a busy lock and retry until the deadline.
    while True:
        try:
            return path.open("a+b")
        except PermissionError:
            if time.monotonic() >= deadline:
                raise TimeoutError("sync_manifest_lock_timeout")
            time.sleep(0.02)


@contextmanager
def _file_lock(path: Path, timeout: float = 30):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with _open_lock_file(path, deadline) as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        acquired = False
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
    renamed_from: str | None = None


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

    def _free_target(self, target: Path, content: bytes | None = None, *, current: Path | None = None,
                     reserved: set[Path] | None = None) -> Path:
        candidate = target
        counter = 2
        while candidate != current and (candidate.exists() or candidate in (reserved or set())):
            if content is not None and candidate.is_file() and candidate.read_bytes() == content:
                return candidate
            candidate = target.with_name(f"{target.stem}-{counter}{target.suffix}")
            counter += 1
        return candidate

    def read(self, source_id: str):
        if not self.path.exists():
            return None
        with _file_lock(self.path.with_suffix(".lock")):
            entries = self._load()["entries"]
            return next((dict(entry) for entry in entries.values()
                         if entry["source_id"] == source_id or source_id in entry.get("aliases", [])), None)

    def write(self, source_id: str, relative_path: str, content: str, *, cursor=None, append: bool = False,
              aliases=(), no_delta: bool = False, content_identity: str | None = None) -> SyncWriteResult:
        proposed = self._target(relative_path)
        encoded = content.encode("utf-8")
        digest = _sha(encoded)
        with _file_lock(self.path.with_suffix(".lock")):
            data = self._load()
            entry = data["entries"].get(source_id)
            reserved = {self._target(other["path"]) for key, other in data["entries"].items() if key != source_id}
            target = self._target(entry["path"]) if entry else self._free_target(proposed, reserved=reserved)
            previous = target.read_bytes() if entry and target.is_file() else None
            if no_delta:
                if not entry or not target.is_file():
                    raise ValueError("sync_no_delta_without_artifact")
                if proposed == target or _sha(previous) != entry["content_sha256"]:
                    return SyncWriteResult(target, "unchanged")
                encoded, digest = previous, entry["content_sha256"]
            operation = "created"
            if entry:
                if not target.exists():
                    operation = "restored_missing"
                elif previous is None or _sha(previous) != entry["content_sha256"]:
                    if append and target.is_file():
                        encoded = _append_markdown(target.read_text(encoding="utf-8"), content).encode("utf-8")
                    update = self._free_target(target.with_name(f"{target.stem}.openmd-update.md"), encoded)
                    if not update.exists():
                        _atomic_write(update, encoded)
                    # Retain the canonical path and last canonical hash: future
                    # runs must continue to protect the user's edited original.
                    return SyncWriteResult(update, "conflict_kept_user_edit")
                elif append and not no_delta:
                    content = _append_markdown(target.read_text(encoding="utf-8"), content)
                    encoded = content.encode("utf-8")
                    digest = _sha(encoded)
                    operation = "updated"
                elif digest == entry["content_sha256"] or (content_identity is not None and content_identity == entry.get("content_identity")):
                    operation = "unchanged"
                    digest = entry["content_sha256"]
                else:
                    operation = "updated"
            old_target = target
            renamed_from = None
            if entry and previous is not None and _sha(previous) == entry["content_sha256"] and proposed != target:
                target = self._free_target(proposed, current=target, reserved=reserved)
                if target != old_target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(old_target, target)
                    renamed_from = old_target.relative_to(self.root).as_posix()
            data["entries"][source_id] = ({**entry, "path": target.relative_to(self.root).as_posix()} if no_delta else {
                "source_id": source_id, "path": target.relative_to(self.root).as_posix(),
                "content_sha256": digest, "cursor": cursor,
                "aliases": sorted(set((entry or {}).get("aliases", [])) | set(aliases)),
                "last_synced": datetime.now(timezone.utc).isoformat(), "schema": SCHEMA,
                **({"content_identity": content_identity} if content_identity is not None else {}),
            })
            try:
                if operation != "unchanged":
                    _atomic_write(target, encoded)
                _atomic_write(self.path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            except OSError:
                if renamed_from is not None:
                    # A failed content/manifest commit must leave the previous
                    # canonical bytes and path retryable under the old entry.
                    if target.read_bytes() == previous:
                        os.replace(target, old_target)
                    else:
                        _atomic_write(old_target, previous)
                        target.unlink()
                raise
            return SyncWriteResult(target, operation, renamed_from)


def _append_markdown(existing: str, incoming: str) -> str:
    """Append conversation body while keeping one front matter/source footer."""
    if not existing.strip():
        return incoming
    # The conversation renderer uses this footer; ## Source is format-only.
    marker = "\n---\n\n来源说明："
    old_footer, new_footer = existing.rfind(marker), incoming.rfind(marker)
    heading_end = incoming.find("\n\n", incoming.find("\n# ") + 1)
    if old_footer >= 0 and new_footer >= 0 and heading_end >= 0:
        return existing[:old_footer].rstrip() + "\n\n" + incoming[heading_end + 2:new_footer].strip() + incoming[new_footer:]
    old_source = existing.find("\n## Source\n")
    new_source = incoming.find("\n## Source\n")
    if old_source < 0 or new_source < 0:
        return existing.rstrip() + "\n\n" + incoming.lstrip()
    old_body = existing[:old_source]
    new_body = incoming[:new_source]
    # Drop the incoming document heading/front matter; retain only new turns.
    heading = new_body.find("\n# ")
    if heading >= 0:
        new_body = new_body[heading:]
    first_break = new_body.find("\n\n")
    if first_break >= 0:
        new_body = new_body[first_break + 2:]
    return old_body.rstrip() + "\n\n" + new_body.strip() + incoming[new_source:]
