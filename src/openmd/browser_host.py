"""Chrome Native Messaging host for the Openmd browser extension.

Chrome starts this process per message (``chrome.runtime.sendNativeMessage``) and talks over
stdin/stdout with 4-byte little-endian length-prefixed JSON. Only the extension id listed in the
host manifest may connect (see ``openmd browser-setup``). There is no network listener.

Saving runs as a detached background job so long transcriptions survive the popup and the
host process. Each job lives in ``<data>/browser-jobs/<id>/status.json``.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openmd.core.preferences import data_directory, load_preferences
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import owned_popen, communicate_interruptibly, kill_process_tree, process_options, terminate_quietly
from openmd.runtime.services import ensure_opencli_daemon

HOST_NAME = "com.openmd.host"
MAX_MESSAGE = 1024 * 1024
RECENT_JOBS = 8
GO_FLAGS = ["--yes", "--enable-network-backends", "--enable-browser-backends", "--enable-media-download", "--enable-asr", "--json"]


# ---------------------------------------------------------------- protocol

def read_message(stream) -> dict[str, Any] | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE:
        raise ValueError("message_too_large")
    payload = stream.read(length)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("message_not_object")
    return value


def write_message(stream, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


# ---------------------------------------------------------------- jobs

def jobs_root() -> Path:
    return data_directory() / "browser-jobs"


def default_save_directory() -> Path:
    last = load_preferences().get("last_save_directory")
    return Path(last) if last else Path.home() / "Openmd"


def _status_path(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef" for ch in job_id):
        raise ValueError("invalid_job_id")
    return jobs_root() / job_id / "status.json"


def read_status(job_id: str) -> dict[str, Any]:
    try:
        return json.loads(_status_path(job_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"id": job_id, "state": "unknown"}


def _write_status(job_dir: Path, status: dict[str, Any]) -> None:
    # The queue runner and status pollers (_recover) may write the same job at
    # once: each writer needs its own temp file, or their bytes can interleave.
    tmp = job_dir / f"status.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, job_dir / "status.json")
    finally:
        tmp.unlink(missing_ok=True)


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("invalid_url")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("unsupported_url: 只能保存 http/https 网页")
    return url.strip()


def _validate_destination(value: object) -> Path:
    if value in (None, ""):
        return default_save_directory()
    if not isinstance(value, str):
        raise ValueError("invalid_save_to")
    path = Path(os.path.expandvars(value.strip().strip('"'))).expanduser()
    if not path.is_absolute():
        raise ValueError("save_to_must_be_absolute: 请填写完整的文件夹路径")
    return path


MAX_BATCH = 200
MAX_ATTEMPTS = 2  # one automatic retry after an interruption (shutdown, crash)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_path() -> Path:
    return jobs_root() / ".runner.lock"


def _runner_alive() -> bool:
    try:
        return _pid_alive(int(_lock_path().read_text(encoding="utf-8").strip() or 0))
    except (OSError, ValueError):
        return False


def _spawn_runner(spawn=subprocess.Popen) -> None:
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW gives the runner a hidden console that every child tool (openmd, opencli,
        # ffmpeg, ASR worker) inherits, so nothing ever flashes on screen. DETACHED_PROCESS must not
        # be combined with it: Windows then ignores CREATE_NO_WINDOW and each console child opens
        # its own visible terminal window.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    command = [sys.executable, "-I", "-m", "openmd.browser_host", "--runner"]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, cwd=str(jobs_root()))
    try:
        # Escape Chrome's job object so closing the browser host does not kill a long save.
        spawn(command, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), **kwargs)
    except OSError:
        spawn(command, creationflags=flags, **kwargs)


_last_seq = 0


def _next_seq() -> int:
    """Strictly increasing within a process, ordered across processes (ns clock)."""
    global _last_seq
    _last_seq = max(time.time_ns(), _last_seq + 1)
    return _last_seq


VIDEO_MODES = ("note", "text", "file")


def _validate_video(video: object) -> str:
    if video in (None, ""):
        return "note"
    if video not in VIDEO_MODES:
        raise ValueError("invalid_video_mode")
    return str(video)


def _enqueue(url: object, destination: Path, batch: str | None = None, video: str = "note") -> dict[str, Any]:
    url = _validate_url(url)
    job_id = uuid.uuid4().hex
    job_dir = jobs_root() / job_id
    job_dir.mkdir(parents=True)
    status = {"id": job_id, "state": "queued", "url": url, "save_to": str(destination), "created": time.time(), "seq": _next_seq(), "attempts": 0, "video": video}
    if batch:
        status["batch"] = batch
    _write_status(job_dir, status)
    return status


def start_job(url: object, save_to: object = None, *, video: object = None, spawn=subprocess.Popen) -> dict[str, Any]:
    status = _enqueue(url, _validate_destination(save_to), video=_validate_video(video))
    _spawn_runner(spawn)
    _prune_old_jobs()
    return status


def start_batch(urls: object, save_to: object = None, *, video: object = None, spawn=subprocess.Popen) -> dict[str, Any]:
    """Queue several links; they run one after another so ASR never overloads the GPU."""
    if not isinstance(urls, list) or not urls:
        raise ValueError("no_urls")
    destination = _validate_destination(save_to)
    video = _validate_video(video)
    batch = uuid.uuid4().hex
    jobs, rejected, seen = [], [], set()
    for url in urls[:MAX_BATCH]:
        key = url.strip() if isinstance(url, str) else url
        if key in seen:
            continue
        seen.add(key)
        try:
            jobs.append(_enqueue(url, destination, batch, video))
        except ValueError as exc:
            rejected.append({"url": str(url)[:200], "error": sanitize_error_summary(exc)})
    if jobs:
        _spawn_runner(spawn)
    _prune_old_jobs()
    return {"batch": batch, "jobs": jobs, "rejected": rejected, "truncated": len(urls) > MAX_BATCH}


def _recover(status: dict[str, Any]) -> dict[str, Any]:
    """A job whose process vanished (shutdown, crash) is re-queued once, then marked failed."""
    if status.get("state") != "running" or _pid_alive(status.get("pid")):
        return status
    attempts = int(status.get("attempts", 1))
    status.pop("pid", None)
    status.pop("child_pid", None)
    if attempts < MAX_ATTEMPTS:
        status.update(state="queued", note="上次被中断（关机或进程结束），已重新排队")
    else:
        status.update(state="failed", error="被中断（关机或进程结束），重试后仍未完成", finished=time.time())
    _write_status(_status_path(status["id"]).parent, status)
    return status


def _queued() -> list[Path]:
    root = jobs_root()
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("*/status.json"):
        try:
            status = _recover(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            continue
        if status.get("state") == "queued":
            found.append((status.get("seq") or int(status.get("created", 0) * 1e9), path.parent))
    return [job_dir for _created, job_dir in sorted(found)]


def ensure_runner(spawn=subprocess.Popen) -> bool:
    """Resume the queue (e.g. after a reboot) when work is waiting and no runner holds the lock."""
    if _queued() and not _runner_alive():
        _spawn_runner(spawn)
        return True
    return False


def _acquire_lock() -> bool:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _runner_alive():
                return False
            lock.unlink(missing_ok=True)  # stale lock from a previous boot
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def run_queue(*, runner=None) -> int:
    """Single runner: process queued jobs oldest-first until none remain."""
    processed = 0
    while True:
        if not _acquire_lock():
            return processed
        try:
            while queued := _queued():
                run_job(queued[0], runner=runner)
                processed += 1
        finally:
            _lock_path().unlink(missing_ok=True)
        # A job queued between the last scan and the unlock is picked up here.
        if not _queued():
            return processed


def _child_env() -> dict[str, str]:
    # openmd prints non-ASCII titles/paths; without this the child writes the ANSI code page
    # (GBK on Chinese Windows) and the job records mojibake paths.
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def _run_openmd(command: list[str], job_dir: Path, status: dict[str, Any]) -> subprocess.CompletedProcess:
    # This runner is outside the per-job owner. A daemon started by the child
    # controller would inherit that outer Job even if detached from its own Job.
    if "--enable-browser-backends" in command and any(command[index:index + 2] == ["-m", "openmd"] for index in range(len(command) - 1)):
        ensure_opencli_daemon(["opencli", "browser-job-read"], env=_child_env(), cwd=str(job_dir))
    process = owned_popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", cwd=str(job_dir), env=_child_env(), **process_options())
    def check_cancelled():
        # The runner owns the Job/group handle. An external host only writes
        # cancellation state; no live root PID is needed to find descendants.
        if read_status(status["id"]).get("state") == "cancelled":
            kill_process_tree(process)

    try:
        status["child_pid"] = process.pid
        _write_status(job_dir, status)
        stdout, stderr = communicate_interruptibly(process, on_poll=check_cancelled)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        terminate_quietly(process)


def run_job(job_dir: Path, *, runner=None) -> dict[str, Any]:
    """Run openmd once for a queued job and record the outcome."""
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    status.update(state="running", started=time.time(), pid=os.getpid(), attempts=int(status.get("attempts", 0)) + 1)
    status.pop("note", None)
    _write_status(job_dir, status)
    command = [sys.executable, "-I", "-X", "utf8", "-m", "openmd", "go", status["url"], *GO_FLAGS, "--video", status.get("video") or "note", "--save-to", status["save_to"]]
    try:
        if runner is None:
            done = _run_openmd(command, job_dir, status)
        else:
            done = runner(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(job_dir), env=_child_env())
        current = read_status(status["id"])
        if current.get("state") == "cancelled":
            return current
        lines = [line for line in (done.stdout or "").splitlines() if line.strip().startswith("{")]
        result = json.loads(lines[-1]) if lines else {}
        if done.returncode == 0 and result.get("ok"):
            saved = result.get("saved_to") or ""
            status.update(state="done", saved_to=saved, title=Path(saved).stem if saved else "")
        else:
            hint = [line for line in (done.stderr or "").splitlines() if line.strip()]
            status.update(state="failed", error=sanitize_error_summary(result.get("error") or (hint[0] if hint else f"exit {done.returncode}")))
    except Exception as exc:  # noqa: BLE001 - a background job must always end with a status
        status.update(state="failed", error=sanitize_error_summary(exc) or "job_failed")
    status["finished"] = time.time()
    status.pop("pid", None)
    status.pop("child_pid", None)
    _write_status(job_dir, status)
    return status


def recent_jobs() -> list[dict[str, Any]]:
    root = jobs_root()
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT_JOBS]:
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return items


def _prune_old_jobs(keep: int = 50) -> None:
    import shutil

    root = jobs_root()
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dirs[keep:]:
        try:
            state = json.loads((old / "status.json").read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            state = None
        if state in {"done", "failed", "cancelled", None}:
            shutil.rmtree(old, ignore_errors=True)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel one job. A running job loses only its own openmd tree; the queue keeps going."""
    status = read_status(job_id)
    if status.get("state") not in {"queued", "running"}:
        return status
    status.update(state="cancelled", finished=time.time())
    status.pop("pid", None)
    status.pop("child_pid", None)
    _write_status(_status_path(job_id).parent, status)
    return status


def pick_folder(initial: object = None) -> dict[str, Any]:
    """Show the native folder picker (topmost) and return the chosen absolute path."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        start = str(initial) if isinstance(initial, str) and Path(initial).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(parent=root, initialdir=start, title="选择 Openmd 保存位置", mustexist=False)
    finally:
        root.destroy()
    if not chosen:
        return {"ok": True, "path": None}
    return {"ok": True, "path": str(Path(chosen).resolve())}


def reveal(job_id: str) -> dict[str, Any]:
    """Open Explorer at a saved file. Only paths recorded by our own jobs are accepted."""
    status = read_status(job_id)
    saved = status.get("saved_to")
    if status.get("state") != "done" or not saved or not Path(saved).exists():
        raise ValueError("nothing_to_reveal")
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(Path(saved))])
    return {"ok": True}


# ---------------------------------------------------------------- dispatch

def handle(message: dict[str, Any]) -> dict[str, Any]:
    kind = message.get("type")
    try:
        if kind == "ping":
            return {"ok": True, "version": 1, "save_to": str(default_save_directory())}
        if kind == "save":
            return {"ok": True, "job": start_job(message.get("url"), message.get("save_to"), video=message.get("video"))}
        if kind == "save_many":
            return {"ok": True, **start_batch(message.get("urls"), message.get("save_to"), video=message.get("video"))}
        if kind == "status":
            status = _recover(read_status(str(message.get("id", ""))))
            ensure_runner()
            return {"ok": True, "job": status}
        if kind == "statuses":
            ids = message.get("ids")
            if not isinstance(ids, list):
                raise ValueError("invalid_ids")
            jobs = {}
            for job_id in ids[:MAX_BATCH]:
                try:
                    jobs[str(job_id)] = _recover(read_status(str(job_id)))
                except ValueError:
                    jobs[str(job_id)] = {"id": str(job_id), "state": "unknown"}
            ensure_runner()
            return {"ok": True, "jobs": jobs}
        if kind == "recent":
            ensure_runner()
            return {"ok": True, "jobs": recent_jobs()}
        if kind == "cancel":
            return {"ok": True, "job": cancel_job(str(message.get("id", "")))}
        if kind == "reveal":
            return reveal(str(message.get("id", "")))
        if kind == "pick_folder":
            return pick_folder(message.get("initial"))
        return {"ok": False, "error": "unknown_message_type"}
    except ValueError as exc:
        return {"ok": False, "error": sanitize_error_summary(exc)}


def serve(stdin=None, stdout=None) -> int:
    if os.name == "nt" and stdin is None:
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except ValueError as exc:
            write_message(stdout, {"ok": False, "error": str(exc)})
            return 1
        if message is None:
            return 0
        write_message(stdout, handle(message))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from openmd.runtime.managed_tools import activate

    activate()  # the queue runner talks to the opencli daemon directly
    if args == ["--runner"]:
        run_queue()
        return 0
    if len(args) == 2 and args[0] == "--run-job":  # jobs started by the first extension build
        run_job(Path(args[1]))
        return 0
    # Chrome passes the caller origin (and on Windows a parent window handle) as arguments.
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
