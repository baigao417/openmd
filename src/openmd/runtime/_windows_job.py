"""Windows Job ownership, including atomic assignment before child execution.

Only imported on Windows. The small Popen creation hook uses STARTUPINFOEX's
JOB_LIST (Windows 10+) so even controller death between CreateProcess and
AssignProcessToJobObject cannot strand a suspended child. No global monkeypatch.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import sys
import threading
import time


kernel = C.WinDLL("kernel32", use_last_error=True)
SIZE = C.c_size_t


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BASIC_LIMITS(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_longlong),
                ("PerJobUserTimeLimit", C.c_longlong), ("LimitFlags", W.DWORD),
                ("MinimumWorkingSetSize", SIZE), ("MaximumWorkingSetSize", SIZE),
                ("ActiveProcessLimit", W.DWORD), ("Affinity", SIZE),
                ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class EXTENDED_LIMITS(C.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE), ("JobMemoryLimit", SIZE),
                ("PeakProcessMemoryUsed", SIZE), ("PeakJobMemoryUsed", SIZE)]


class BASIC_ACCOUNTING(C.Structure):
    _fields_ = [(name, C.c_longlong) for name in ("TotalUserTime", "TotalKernelTime",
                    "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
                (name, W.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses",
                    "ActiveProcesses", "TotalTerminatedProcesses")]


class STARTUPINFO(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR),
                ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                *[(name, W.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize",
                    "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")],
                ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", C.c_void_p), ("hStdInput", W.HANDLE),
                ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", C.c_void_p)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


def _api(name, result, *args):
    function = getattr(kernel, name)
    function.restype, function.argtypes = result, args
    return function


CreateJobObject = _api("CreateJobObjectW", W.HANDLE, C.c_void_p, W.LPCWSTR)
SetInformationJobObject = _api("SetInformationJobObject", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD)
AssignProcessToJobObject = _api("AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
TerminateJobObject = _api("TerminateJobObject", W.BOOL, W.HANDLE, W.UINT)
QueryInformationJobObject = _api("QueryInformationJobObject", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p)
CloseHandle = _api("CloseHandle", W.BOOL, W.HANDLE)
ResumeThread = _api("ResumeThread", W.DWORD, W.HANDLE)
TerminateProcess = _api("TerminateProcess", W.BOOL, W.HANDLE, W.UINT)
InitializeProcThreadAttributeList = _api("InitializeProcThreadAttributeList", W.BOOL, C.c_void_p, W.DWORD, W.DWORD, C.POINTER(SIZE))
UpdateProcThreadAttribute = _api("UpdateProcThreadAttribute", W.BOOL, C.c_void_p, W.DWORD, SIZE, C.c_void_p, SIZE, C.c_void_p, C.c_void_p)
DeleteProcThreadAttributeList = _api("DeleteProcThreadAttributeList", None, C.c_void_p)
CreateProcess = _api("CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p,
                     W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR,
                     C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION))
OpenThread = _api("OpenThread", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
CancelSynchronousIo = _api("CancelSynchronousIo", W.BOOL, W.HANDLE)


def _checked(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.handle = _checked(CreateJobObject(None, None))
        try:
            limits = EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            _checked(SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.handle:
                handle, self.handle = self.handle, None
                try:
                    # Terminate explicitly; closing also protects controller crashes.
                    _checked(TerminateJobObject(handle, 1))
                    # Termination is asynchronous. Give file handles/cwds a
                    # bounded release window before file-backed workspaces close.
                    deadline = time.monotonic() + 2
                    info = BASIC_ACCOUNTING()
                    while time.monotonic() < deadline:
                        _checked(QueryInformationJobObject(handle, 1, C.byref(info), C.sizeof(info), None))
                        if info.ActiveProcesses == 0:
                            break
                        time.sleep(.01)
                finally:
                    CloseHandle(handle)

    def __del__(self):
        if getattr(self, "handle", None):
            CloseHandle(self.handle)


def cancel_thread_io(thread):
    if thread is not None and thread.is_alive() and thread.native_id:
        handle = OpenThread(0x0001, False, thread.native_id)  # THREAD_TERMINATE
        if handle:
            try:
                CancelSynchronousIo(handle)
            finally:
                CloseHandle(handle)


class JobPopen(subprocess.Popen):
    """Keep Popen's pipe/text/wait implementation; customize only OS creation.

    Signature follows CPython's Windows Popen._execute_child; tested on 3.12.
    Creation errors fail closed: never silently launch without Job ownership.
    """

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds,
                       cwd, env, startupinfo, creationflags, shell,
                       p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                       *unused, **unused_kwargs):
        if pass_fds:
            raise ValueError("pass_fds not supported on Windows")
        if isinstance(args, (bytes, os.PathLike)):
            if shell:
                raise TypeError("bytes/path-like args is not allowed with shell")
            args = subprocess.list2cmdline([args])
        elif not isinstance(args, str):
            args = subprocess.list2cmdline(args)
        executable = os.fsdecode(executable) if executable is not None else None
        startupinfo = startupinfo.copy() if startupinfo is not None else subprocess.STARTUPINFO()
        if startupinfo.lpAttributeList and set(startupinfo.lpAttributeList) - {"handle_list"}:
            raise ValueError("Unsupported Windows startup attribute")
        native = STARTUPINFOEX()
        native.StartupInfo.cb = C.sizeof(native)
        native.StartupInfo.dwFlags = startupinfo.dwFlags
        native.StartupInfo.wShowWindow = startupinfo.wShowWindow
        handles = list((startupinfo.lpAttributeList or {}).get("handle_list", []))
        if -1 not in (p2cread, c2pwrite, errwrite):
            native.StartupInfo.dwFlags |= 0x100  # STARTF_USESTDHANDLES
            native.StartupInfo.hStdInput = int(p2cread)
            native.StartupInfo.hStdOutput = int(c2pwrite)
            native.StartupInfo.hStdError = int(errwrite)
            handles += [int(p2cread), int(c2pwrite), int(errwrite)]
        handles = self._filter_handle_list(handles)
        if shell:
            native.StartupInfo.dwFlags |= 1
            native.StartupInfo.wShowWindow = 0
            executable = executable or os.environ.get("ComSpec") or os.path.join(os.environ.get("SystemRoot", ""), "System32", "cmd.exe")
            if not os.path.isabs(executable):
                raise FileNotFoundError("An absolute Windows shell path is required")
            args = f'{executable} /c "{args}"'
        cwd = os.fsdecode(cwd) if cwd is not None else None
        environment = None
        if env is not None:
            entries = []
            for key, value in sorted(env.items(), key=lambda pair: pair[0].upper()):
                if "\0" in key or "\0" in value or "=" in key[1:] or not key:
                    raise ValueError("illegal environment variable")
                entries.append(f"{key}={value}")
            environment = C.create_unicode_buffer("\0".join(entries) + "\0\0")
        size = SIZE()
        InitializeProcThreadAttributeList(None, 2 if handles else 1, 0, C.byref(size))
        attributes = C.create_string_buffer(size.value)
        _checked(InitializeProcThreadAttributeList(attributes, 2 if handles else 1, 0, C.byref(size)))
        native.lpAttributeList = C.cast(attributes, C.c_void_p)
        info = PROCESS_INFORMATION()
        try:
            # JOB_LIST assigns at CreateProcess itself, closing the crash window.
            jobs = (W.HANDLE * 1)(self._openmd_owner.handle)
            _checked(UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, C.sizeof(jobs), None, None))
            if handles:
                inherited = (W.HANDLE * len(handles))(*handles)
                _checked(UpdateProcThreadAttribute(attributes, 0, 0x20002, inherited, C.sizeof(inherited), None, None))
            sys.audit("subprocess.Popen", executable, args, cwd, env)
            _checked(CreateProcess(executable, C.create_unicode_buffer(args), None, None,
                       bool(handles) or not close_fds,
                       creationflags | 0x4 | 0x80000 | 0x400,  # SUSPENDED/EXTENDED/UNICODE
                       environment, cwd, C.byref(native), C.byref(info)))
            self._child_created = True
            self._handle = subprocess.Handle(info.hProcess)
            self.pid = info.dwProcessId
            # Already in this Job atomically; explicit assignment validates it
            # before any child code can run or spawn a grandchild.
            _checked(AssignProcessToJobObject(self._openmd_owner.handle, info.hProcess))
            if ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
        except BaseException:
            if info.hProcess:
                TerminateProcess(info.hProcess, 1)
            raise
        finally:
            if info.hThread:
                CloseHandle(info.hThread)
            DeleteProcThreadAttributeList(attributes)
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

    def __init__(self, *args, **kwargs):
        self._openmd_owner = Job()
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            self._openmd_owner.close()
            raise
