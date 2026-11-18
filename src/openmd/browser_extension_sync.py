"""Shared content identity and transactional publication of the managed extension.

Chrome loads the stable path. Stage a complete tree on the same volume, serialize
publishers, and rename whole trees (never copy files over a running extension).
Windows cannot atomically replace a nonempty directory: the two renames have a
brief missing-path interval, but no mixed-version tree is ever published.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def extension_content_hash(directory: Path) -> str:
    """SHA256 of sorted POSIX names, NUL, file SHA256, newline; no Python cache.

    Missing or unreadable trees raise OSError, so callers cannot mistake a failed
    check for an up-to-date installation. Reusable by read-only diagnostics.
    """
    if _is_link(directory) or not directory.is_dir():
        raise OSError("extension_directory_must_be_a_real_directory")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix in {".py", ".pyc"}:
            continue
        if _is_link(path):
            raise OSError("extension_tree_must_not_contain_links")
        if path.is_file():
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{relative.as_posix()}\0{content}\n".encode("utf-8"))
    return digest.hexdigest()


@contextlib.contextmanager
def _publish_lock(root: Path):
    # OS locks release after process death. The persistent lock file is not an
    # extension asset and must never be removed while another host may wait.
    with (root / ".extension-sync.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 30
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("extension_sync_lock_timeout") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def sync_extension(source: Path, target: Path) -> tuple[bool, str]:
    """Return (updated, content_hash), preserving the previous tree on failure."""
    wanted = extension_content_hash(source)
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    with _publish_lock(root):
        previous = root / ".extension-previous"
        # Recover an interrupted publisher before comparing or discarding trees.
        if previous.exists() and not target.exists():
            previous.replace(target)
        if target.exists() and extension_content_hash(target) == wanted:
            return False, wanted
        if previous.exists():
            if _is_link(previous):
                raise OSError("extension_backup_must_not_be_a_link")
            shutil.rmtree(previous)
        with tempfile.TemporaryDirectory(prefix=".extension-stage-", dir=root) as temporary:
            stage = Path(temporary) / "extension"
            shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc"))
            if extension_content_hash(stage) != wanted:
                raise OSError("extension_package_changed_during_sync")
            had_previous = target.exists()
            if had_previous:
                target.replace(previous)
            try:
                stage.replace(target)
            except OSError:
                if had_previous:
                    previous.replace(target)
                raise
        if previous.exists():
            shutil.rmtree(previous)
        return True, wanted
