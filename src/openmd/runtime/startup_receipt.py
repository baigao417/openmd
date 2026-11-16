"""Require a real session-start event within a bounded launch interval."""
import json
import queue
import re
import threading
import time


def wait_for_session(process, *, timeout: float = 30) -> dict:
    if not 0 < timeout <= 60 or process.stdout is None:
        raise ValueError('invalid_startup_check')
    messages = queue.Queue(maxsize=128)
    stopped = threading.Event()
    def read():
        try:
            while not stopped.is_set():
                line = process.stdout.readline(4097)
                if not line:
                    break
                try:
                    messages.put_nowait(line)
                except queue.Full:
                    break
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    start = time.monotonic()
    try:
        for _ in range(128):
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                break
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                raise ValueError('startup_exited_without_session')
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            session = event.get('thread_id') if isinstance(event, dict) and event.get('type') == 'thread.started' else None
            if isinstance(session, str) and re.fullmatch(r'[a-fA-F0-9-]{36}', session):
                return {'schema': 'openmd.startup-receipt/v1', 'session_id': session, 'seconds': round(time.monotonic()-start, 3)}
        raise TimeoutError('startup_session_deadline')
    finally:
        stopped.set()
