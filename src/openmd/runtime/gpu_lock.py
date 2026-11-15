"""Interruptible cross-process GPU lease. The OS, not a PID file, owns the lock."""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path


def gpu_lock_path() -> Path:
    root = Path(os.environ.get("OPENMD_RUNTIME_DIR", str(Path.home() / ".openmd/runtime"))).expanduser()
    return root.resolve() / "gpu.lock"


def release_torch_cache() -> None:
    """Release allocations before handing the lease to another GPU backend."""
    import gc
    import sys

    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


@contextmanager
def gpu_lock(*, timeout: float | None = None, path: Path | None = None, check_quarantine: bool = True):
    """Lock one persistent byte; never unlink it (unlinking creates split locks).

    msvcrt/flock release locks on process death, including forced termination.
    A stale file is therefore harmless and can be reused without deleting it.
    Short polling keeps Windows Ctrl+C responsive while waiting for the lease.
    """
    limit = float(os.getenv("OPENMD_GPU_LOCK_TIMEOUT", "600")) if timeout is None else timeout
    if not 0 <= limit < float("inf"):
        raise ValueError("gpu_lock_timeout_invalid")
    target = path or gpu_lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as stream:
        if target.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        acquired = False
        deadline = time.monotonic() + limit
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
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("gpu_lock_timeout: another Openmd OCR/ASR process holds the GPU") from exc
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            from .gpu_quarantine import recover_quarantine

            if check_quarantine:
                recover_quarantine(target, deadline=deadline)
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
