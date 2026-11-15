from __future__ import annotations

import subprocess
import sys

import pytest

from openmd.runtime.interpreters import wait_interruptibly


class _FakeProcess:
    args = ["fake"]

    def __init__(self, timeouts_before_exit: int, *, interrupt_on: int | None = None) -> None:
        self.calls: list[float] = []
        self._timeouts = timeouts_before_exit
        self._interrupt_on = interrupt_on

    def poll(self) -> int | None:
        self.calls.append(0)
        if self._interrupt_on is not None and len(self.calls) == self._interrupt_on:
            raise KeyboardInterrupt
        if len(self.calls) <= self._timeouts:
            return None
        return 0


def test_waits_in_short_slices_until_exit():
    process = _FakeProcess(timeouts_before_exit=3)
    assert wait_interruptibly(process, 300, poll_s=0.5) == 0
    assert len(process.calls) == 4
    assert all(call <= 0.5 for call in process.calls)


def test_keyboard_interrupt_between_slices_propagates(monkeypatch):
    import openmd.runtime.interpreters as runtime
    killed = []
    monkeypatch.setattr(runtime, "terminate_quietly", lambda child: killed.append(child))
    process = _FakeProcess(timeouts_before_exit=100, interrupt_on=2)
    with pytest.raises(KeyboardInterrupt):
        wait_interruptibly(process, 300, poll_s=0.5)
    assert len(process.calls) == 2
    assert killed == [process]


def test_overall_timeout_still_raises():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            wait_interruptibly(child, 0.3, poll_s=0.1)
    finally:
        child.kill()
        child.wait(timeout=10)
