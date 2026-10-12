"""Placeholder for a future FunASR SenseVoice local backend.

The V1 lane intentionally does not import FunASR at module import time.  A real
provider can be added behind an explicit backend gate after local dependency
smoke testing.
"""

from __future__ import annotations

from openmd.adapters.local_audio.asr import MissingASRBackend


def create_backend() -> MissingASRBackend:
    return MissingASRBackend("funasr.sensevoice")
