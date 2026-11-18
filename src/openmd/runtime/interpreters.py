"""Choose and probe an existing ASR interpreter without changing environments."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openmd.core.local_config import load_local_config
from openmd.core.secrets import sanitize_error_summary
from .models import checkout_root

from openmd.runtime.tempdirs import note_process_owner, owned_temporary_directory


REQUIRED_MODULES = ("torch", "ctranslate2", "faster_whisper", "qwen_asr", "funasr", "soundfile", "opencc")
_PROBE_CACHE: dict[tuple[object, ...], dict[str, Any]] = {}


@dataclass(frozen=True)
class AsrRuntime:
    mode: str
    python: Path | None
    reason: str
    probe: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "python": display_path(self.python), "reason": self.reason, "probe": self.probe}


def display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    value = str(path)
    home = str(Path.home())
    return "~" + value[len(home):] if value.lower().startswith(home.lower() + os.sep) else value


def worker_environment() -> dict[str, str]:
    """Only local-runtime settings cross the process boundary; no cloud credentials."""
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE", "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "OPENMD_CONFIG", "OPENMD_MODEL_ROOTS", "OPENMD_PREPARED_MODEL_ROOT", "OPENMD_ASR_CHINESE_SCRIPT", "OPENMD_RUNTIME_DIR", "OPENMD_GPU_LOCK_TIMEOUT"}
    prefixes = ("OPENMD_FASTER_WHISPER_", "OPENMD_ASR_AUTO_", "OPENMD_ASR_ORIGINAL_PEAK_", "OPENMD_ASR_PREPARED_PEAK_", "OPENMD_ASR_LANGUAGE_PROBE_PEAK_", "OPENMD_ASR_MARGIN_", "OPENMD_QWEN3_", "OMP_", "MKL_", "KMP_")
    env = {key: value for key, value in os.environ.items() if (key.upper() in keep or key.upper().startswith(prefixes)) and not re.search(r"TOKEN|COOKIE|SECRET|PASSWORD|CREDENTIAL|API.?KEY", key, re.I)}
    if os.environ.get('OPENMD_OPENCLI_CANARY') == '1':
        for key in ('OPENMD_OPENCLI_CANARY', 'OPENMD_OPENCLI_CANARY_PACKAGE', 'NODE_OPTIONS'):
            if key in os.environ:
                env[key] = os.environ[key]
    # The worker cwd is deliberately unrelated to the caller. Resolve caller-owned
    # relative locations before crossing that boundary; repository IDs stay IDs.
    for key in ("OPENMD_CONFIG", "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE", "OPENMD_RUNTIME_DIR", "OPENMD_PREPARED_MODEL_ROOT"):
        if env.get(key):
            env[key] = str(Path(env[key]).expanduser().resolve())
    if env.get("OPENMD_MODEL_ROOTS"):
        env["OPENMD_MODEL_ROOTS"] = os.pathsep.join(str(Path(part).expanduser().resolve()) for part in env["OPENMD_MODEL_ROOTS"].split(os.pathsep) if part)
    for key in ("OPENMD_FASTER_WHISPER_MODEL", "OPENMD_ASR_AUTO_EN_MODEL", "OPENMD_QWEN3_MODEL", "OPENMD_QWEN3_VAD_MODEL"):
        value = env.get(key)
        if value:
            path = Path(value).expanduser()
            if path.is_dir() or path.is_absolute() or value.startswith((".", "~")) or "\\" in value:
                env[key] = str(path.resolve())
    env.update({"PYTHONIOENCODING": "utf-8", "OPENMD_ASR_WORKER": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "MODELSCOPE_OFFLINE": "1"})
    return env


def worker_command(python: Path, *args: str, module: str = "openmd.runtime.asr_worker") -> list[str]:
    """Load only this Openmd package; worker dependencies belong to its interpreter.

    Adding the controller's site-packages to PYTHONPATH shadows a worker's CUDA
    Torch with the controller's CPU build after a wheel installation. An explicit
    package spec preserves resources/__file__ without exposing sibling packages.
    Isolated mode also ignores user PYTHONPATH and the caller's working directory.
    """
    bootstrap = (
        "import importlib.util,runpy,sys; "
        "p=sys.argv.pop(1); "
        "s=importlib.util.spec_from_file_location('openmd',p); "
        "m=importlib.util.module_from_spec(s); sys.modules['openmd']=m; "
        "s.loader.exec_module(m); "
        f"runpy.run_module({module!r},run_name='__main__')"
    )
    return [str(python), "-I", "-B", "-c", bootstrap, str(Path(__file__).resolve().parents[1] / "__init__.py"), *args]


class _ProcessGroup:
    def __init__(self, pid):
        self.pid = pid
        self.closed = False

    def close(self):
        if not self.closed:
            # The group remains ours after its leader exits. Do not poll the
            # leader, or reconstruct a new tree from possibly recycled PIDs.
            self.closed = True
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def owned_popen(*args, **kwargs):
    """Establish subtree ownership before any worker code can execute.

    Windows creation is atomically in a kill-on-close Job, suspended until
    assignment has been checked; POSIX creates a new session/process group.
    Keep the owner attached even after poll reports that the leader exited.
    """
    # A persistent browser bridge must start before the per-command owner exists.
    # This helper recognizes only OpenCLI; every actual command still owns its tree.
    from .services import bridge_profile_env, ensure_opencli_daemon

    command = args[0] if args else kwargs.get("args")
    from .managed_tools import command_environment, managed_command
    if isinstance(command, (list, tuple)) and command:
        name = Path(command[0]).name.lower()
        if name in {"opencli", "opencli.cmd", "opencli.exe", "opencli.ps1", "opencli.bat"}:
            command = managed_command(list(command[1:]))
            if args:
                args = (command, *args[1:])
            else:
                kwargs["args"] = command
            kwargs["env"] = command_environment(kwargs.get("env"))
        elif len(command) > 1 and Path(command[1]).as_posix().endswith("/@jackwener/opencli/dist/src/main.js"):
            kwargs["env"] = command_environment(kwargs.get("env"))
    ensure_opencli_daemon(command, env=kwargs.get("env"), cwd=kwargs.get("cwd"))
    selected = bridge_profile_env(command, kwargs.get("env"))
    if selected is not None:
        kwargs["env"] = selected
    if os.name == "nt":
        from ._windows_job import JobPopen

        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        process = JobPopen(*args, **kwargs)
    else:
        kwargs["start_new_session"] = True
        if kwargs.get("process_group") not in (None, -1):
            raise ValueError("Owned children must use their new session's process group")
        process = subprocess.Popen(*args, **kwargs)
        process._openmd_owner = _ProcessGroup(process.pid)
    try:
        note_process_owner(process)
    except BaseException:
        terminate_quietly(process)
        raise
    return process


def wait_interruptibly(process: subprocess.Popen, timeout: float | None, *, poll_s: float = 0.2, cleanup_on_error: bool = True, cleanup_on_exit: bool = True, on_poll=None) -> int:
    """Wait for a child while staying responsive to Ctrl+C.

    Windows Popen.wait/communicate both have a blocking SIGINT grace wait.
    Poll the process and sleep in short Python slices so no subprocess signal
    handler can turn a negative remaining timeout into an infinite OS wait.
    Raises ``subprocess.TimeoutExpired`` like ``Popen.wait``.
    """
    note_process_owner(process)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            if on_poll is not None:
                on_poll()
            result = process.poll()
            if result is not None:
                if cleanup_on_exit and getattr(process, "__dict__", {}).get("_openmd_owner") is not None:
                    kill_process_tree(process)
                return result
            remaining = poll_s if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            # Popen.wait() has the same negative SIGINT grace-time bug as
            # communicate() on Windows 3.12. poll() never blocks; Python sleep
            # returns to the interpreter without subprocess's SIGINT handler.
            time.sleep(min(poll_s, remaining))
    except BaseException:
        if cleanup_on_error:
            terminate_quietly(process)
        raise


def communicate_interruptibly(process: subprocess.Popen, timeout: float | None = None, *, input=None, poll_s: float = 0.2, on_poll=None):
    """Drain pipes off the main thread; keep all signal handling in the short wait.

    Windows Python 3.12 communicate() catches KeyboardInterrupt and may pass a
    negative remaining timeout to WaitForSingleObject (INFINITE when -1 ms).
    Running that I/O method on a daemon thread avoids its signal handler entirely,
    including a blocking stdin write. The main thread uses our existing waiter.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    finished = threading.Event()
    output = []
    errors = []
    cleaned = False

    def exchange():
        try:
            output.append(process.communicate(input=input))
        except BaseException as exc:
            errors.append(exc)
        finally:
            # Stream close can take the buffer lock. It must never run on the
            # controller thread while a pipe reader is blocked holding it.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            finished.set()

    io_thread = threading.Thread(target=exchange, name="openmd-process-io", daemon=True)
    io_thread.start()
    try:
        wait_interruptibly(process, timeout, poll_s=poll_s, cleanup_on_error=False, cleanup_on_exit=False, on_poll=on_poll)
        # Descendants can hold pipes open after their parent exits. The same
        # overall deadline covers pipe draining, as subprocess.communicate does.
        while not finished.is_set():
            if on_poll is not None:
                on_poll()
            remaining = poll_s if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            finished.wait(min(poll_s, remaining))
        if errors:
            raise errors[0]
        return output[0]
    except BaseException as exc:
        cleaned = True
        terminate_quietly(process)
        # A killed tree releases pipes. Join in bounded slices before closing
        # handles, without letting cleanup replace the original cancellation.
        cleanup_deadline = time.monotonic() + 2
        while not finished.is_set() and time.monotonic() < cleanup_deadline:
            finished.wait(poll_s)
        if not finished.is_set() and os.name == "nt":
            from ._windows_job import cancel_thread_io

            # communicate uses additional reader threads on Windows. Cancel a
            # blocked write/read as a bounded fallback after terminating the Job.
            for thread in (io_thread, getattr(process, "_stdout_thread", None), getattr(process, "_stderr_thread", None)):
                cancel_thread_io(thread)
            finished.wait(poll_s)
        if not finished.is_set() and hasattr(exc, "add_note"):
            exc.add_note("Owned process terminated; pipe I/O did not finish within the 2.2s cleanup budget")
        if isinstance(exc, subprocess.TimeoutExpired) and output:
            exc.stdout, exc.stderr = output[0]
            if os.name != "nt":
                exc.stdout = exc.stdout.encode(process.encoding or "utf-8") if isinstance(exc.stdout, str) else exc.stdout
                exc.stderr = exc.stderr.encode(process.encoding or "utf-8") if isinstance(exc.stderr, str) else exc.stderr
        raise
    finally:
        # Normal, timeout and cancellation all close the same launch-time owner.
        if not cleaned:
            terminate_quietly(process)


def run_interruptibly(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """subprocess.run contract with short waits and owned-tree cleanup on cancellation/timeout."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = owned_popen(*popenargs, **kwargs)
    try:
        stdout, stderr = communicate_interruptibly(process, timeout, input=input)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    finally:
        terminate_quietly(process)


def terminate_quietly(process: subprocess.Popen, *, wait_s: float = 2) -> None:
    """Kill the owned tree without letting cleanup errors replace the caller's exception."""
    try:
        kill_process_tree(process)
        wait_interruptibly(process, wait_s, cleanup_on_error=False, cleanup_on_exit=False)
    except Exception:
        pass


def remove_tree_with_retry(path: Path, *, attempts: int = 40, delay_s: float = 0.25) -> bool:
    """Best-effort removal that tolerates handles released a moment after a tree kill."""
    import shutil

    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(delay_s)
    return False


def kill_process_tree(process: subprocess.Popen | int) -> None:
    """Kill an owned child tree (or a recorded browser-job child PID)."""
    pid = process if isinstance(process, int) else process.pid
    owner = None if isinstance(process, int) else getattr(process, "__dict__", {}).get("_openmd_owner")
    if owner is not None:
        owner.close()
        return
    if not isinstance(process, int) and process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        killer = subprocess.Popen([str(taskkill), "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_interruptibly(killer, 15, cleanup_on_error=False)
        except BaseException:
            killer.kill()
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not isinstance(process, int) and process.poll() is None:
        process.kill()


def process_options() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def _ready_for(report: dict[str, Any], backend: str) -> bool:
    return bool(report.get("backend_readiness", {}).get(backend, report.get("ready", False)))


def probe_asr_runtime(python: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Probe metadata, actual CUDA visibility and local models in an isolated process."""
    try:
        stamp = python.stat().st_mtime_ns
    except OSError:
        return {"ready": False, "error_code": "asr_runtime_missing", "python": display_path(python)}
    env = worker_environment()
    config = load_local_config()
    key = (str(python.resolve()), stamp, json.dumps(config, sort_keys=True), tuple(sorted((k, v) for k, v in env.items() if k.startswith(("OPENMD_", "HF_", "MODELSCOPE_", "CUDA_")))))
    if not refresh and key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    command = worker_command(python, "--probe")
    try:
        with owned_temporary_directory(prefix="openmd-asr-probe-") as workdir:
            process = owned_popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=workdir, **process_options())
            try:
                stdout, stderr = communicate_interruptibly(process, timeout=60)
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                report = {"ready": False, "error_code": "asr_runtime_probe_timeout", "python": display_path(python)}
            except BaseException:
                kill_process_tree(process)
                raise
            else:
                if process.returncode or len(stdout) > 1024 * 1024:
                    raise ValueError(sanitize_error_summary(stderr[-2048:].decode("utf-8", errors="replace")) or "probe exited without a valid report")
                report = json.loads(stdout.decode("utf-8-sig"))
                if not isinstance(report, dict) or report.get("schema") != "openmd.asr-probe/v1" or not isinstance(report.get("ready"), bool):
                    raise ValueError("invalid ASR probe schema")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report = {"ready": False, "error_code": "asr_runtime_probe_failed", "python": display_path(python), "error_summary": sanitize_error_summary(exc)}
    _PROBE_CACHE[key] = report
    return report


def select_asr_runtime(*, refresh: bool = False, backend: str = "asr.auto") -> AsrRuntime:
    if os.getenv("OPENMD_ASR_WORKER") == "1":
        return AsrRuntime("in_process", Path(sys.executable), "ASR worker: recursive handoff disabled")
    config = load_local_config()
    explicit = os.getenv("OPENMD_ASR_PYTHON") or config.get("asr_python")
    if explicit:
        selected = Path(explicit).expanduser()
        if not selected.is_absolute():
            selected = (checkout_root() or Path.cwd()) / selected
        report = probe_asr_runtime(selected, refresh=refresh)
        mode = "in_process" if selected.resolve() == Path(sys.executable).resolve() else "worker"
        ready = _ready_for(report, backend)
        return AsrRuntime(mode if ready else "unavailable", selected, "explicit ASR runtime" if ready else "Configured ASR runtime is not ready; run openmd doctor --profile media", report)

    candidates: list[Path] = []
    try:
        current_has_stack = all(importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES)
    except (ValueError, ImportError):
        current_has_stack = False
    if current_has_stack:
        candidates.append(Path(sys.executable))
    root = checkout_root()
    if root:
        candidates.append(root / "out" / "asr-bench" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    reports: list[dict[str, Any]] = []
    for candidate in dict.fromkeys(candidates):
        report = probe_asr_runtime(candidate, refresh=refresh)
        reports.append(report)
        if _ready_for(report, backend):
            mode = "in_process" if candidate.resolve() == Path(sys.executable).resolve() else "worker"
            return AsrRuntime(mode, candidate, "Selected a local CUDA ASR runtime with all required models", report)
    # A timeout or crashed probe is unknown readiness, not evidence that the GPU
    # stack is absent. Do not silently change engine/device on transient failure.
    if any(report.get("error_code") in {"asr_runtime_probe_timeout", "asr_runtime_probe_failed"} for report in reports):
        return AsrRuntime("unavailable", None, "Local ASR runtime probe did not complete; retry openmd doctor --profile media before transcription", {"ready": False, "attempts": reports})
    # Retain the documented CPU Whisper path only for confirmed missing stacks.
    if backend in {"asr.auto", "asr.faster_whisper"}:
        try:
            from .models import configured_models

            local_models = configured_models()
            required = ("whisper-large-v3", "whisper-large-v3-turbo") if backend == "asr.auto" else ("whisper-large-v3",)
            if importlib.util.find_spec("faster_whisper") is not None and all(local_models[key].path is not None for key in required):
                return AsrRuntime("in_process", Path(sys.executable), "No complete CUDA worker is ready; using the documented local Whisper fallback", {"ready": False, "fallback_available": True, "attempts": reports})
        except (ImportError, ValueError):
            pass
    return AsrRuntime("unavailable", None, "No complete local ASR runtime is ready; configure asr_python and local models, then run openmd doctor --profile media", {"ready": False, "attempts": reports})
