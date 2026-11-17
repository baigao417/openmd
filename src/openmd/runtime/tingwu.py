"""Explicit cloud ASR of an already downloaded audio file via OpenCLI.

No platform sessions, signed locators or API credentials cross this interface.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_asr_execution
from openmd.adapters.local_audio.asr import ASRBackend
from .interpreters import run_interruptibly


def probe_audio_duration(path: Path) -> float | None:
    executable = shutil.which('ffprobe')
    if not executable:
        return None
    try:
        done = run_interruptibly([executable, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True, text=True, timeout=30)
        duration = float(done.stdout.strip()) if done.returncode == 0 else 0
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def select_xiaoe_asr(duration: float | None, setting: dict, *, allow_cloud: bool = False) -> dict:
    threshold = setting.get('threshold_seconds', 600)
    known = type(duration) in (int, float) and math.isfinite(duration) and duration > 0
    authorized = allow_cloud or setting.get('enabled') is True
    reason = ('cloud_not_authorized' if not authorized else 'duration_unknown' if not known
              else 'below_threshold' if duration < threshold
              else 'explicit_cloud_gate' if allow_cloud else 'local_long_course_opt_in')
    return {'selected_backend': 'asr.tingwu' if authorized and known and duration >= threshold else 'asr.auto',
            'reason': reason, 'duration_seconds': duration if known else None, 'threshold_seconds': threshold}


ERRORS = {
    'cloud_not_authorized': 'Tingwu requires explicit cloud authorization or the local Xiaoe long-course opt-in.',
    'tingwu_audio_invalid': 'Tingwu accepts only an existing audio file with a positive independently probed duration.',
    'tingwu_auth_required': 'Tingwu is not signed in; sign in through your browser and retry.',
    'tingwu_browser_unavailable': 'The OpenCLI browser session is unavailable; connect the Browser Bridge and retry.',
    'tingwu_upload_timeout': 'Tingwu upload did not become ready before its deadline.',
    'tingwu_submission_timeout': 'Tingwu did not create the matching server record before its deadline.',
    'tingwu_server_failed': 'Tingwu reported a failed upload or transcription.',
    'tingwu_identity_mismatch': 'Tingwu returned a record with a different upload identity.',
    'tingwu_coverage_incomplete': 'Tingwu did not return complete paragraphs covering at least 98 percent of the audio.',
    'tingwu_protocol_invalid': 'Tingwu returned an invalid response.',
    'tingwu_failed': 'Tingwu failed; no other cloud or local backend was used as a fallback.',
}


class TingwuASRBackend(ASRBackend):
    name = 'asr.tingwu'

    def __init__(self, *, allow_cloud: bool = False, runner=None, duration_reader=None, timeout: int = 1800):
        self.allow_cloud = allow_cloud
        self.runner = runner
        self.duration_reader = duration_reader or probe_audio_duration
        self.timeout = timeout

    def _failed(self, code: str, seconds: float = 0, phases=None) -> TranscriptResult:
        return TranscriptResult(text='', status='failed', source_level='partial_extraction', backend=self.name,
            error_code=code, error_summary=ERRORS[code], source_notes=['Explicit Tingwu cloud ASR; no fallback was attempted.'],
            metadata={'asr_backend': self.name, 'elapsed_seconds': seconds, **sanitize_asr_execution({'cloud_phase_seconds':phases})})

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        if not self.allow_cloud:
            return self._failed('cloud_not_authorized')
        path = Path(audio_path).resolve()
        duration = self.duration_reader(path) if path.is_file() else None
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            return self._failed('tingwu_audio_invalid')
        from .opencli_adapters import run_tingwu
        start = time.monotonic()
        try:
            code, stdout, _ = (self.runner or run_tingwu)(path, duration=duration, timeout=self.timeout)
            data = json.loads(stdout)
            elapsed = round(time.monotonic() - start, 3)
            if not isinstance(data, dict):
                return self._failed('tingwu_protocol_invalid', elapsed)
            if code or data.get('ok') is not True:
                error = data.get('error_code')
                return self._failed(error if error in ERRORS else 'tingwu_failed', elapsed, data.get('phase_seconds'))
            segments, end = data.get('segments'), data.get('coverage_end_seconds')
            if (not isinstance(segments, list) or not segments or len(segments) > 100000
                    or type(end) not in (int, float) or not math.isfinite(end) or not .98 <= end / duration <= 1.02
                    or any(not isinstance(s, dict) or not isinstance(s.get('text'), str) or not s['text'].strip()
                           or type(s.get('start_seconds')) not in (int, float) or not math.isfinite(s['start_seconds'])
                           or not 0 <= s['start_seconds'] <= end for s in segments)):
                return self._failed('tingwu_protocol_invalid', elapsed)
            lines = []
            for segment in segments:
                seconds = int(segment['start_seconds'])
                stamp = f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
                lines.append(f'[{stamp}] {segment["text"]}')
            return TranscriptResult(text='\n\n'.join(lines), status='verified', source_level='media_asr', backend=self.name,
                source_notes=['Tingwu cloud ASR of core-downloaded audio; exact upload identity and full API paragraph coverage checked.'],
                metadata={'asr_backend': self.name, 'duration': duration, 'elapsed_seconds': elapsed,
                          'segment_count': len(segments), 'coverage_end_seconds': end,
                          **sanitize_asr_execution({'cloud_phase_seconds':data.get('phase_seconds')})})
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
            return self._failed('tingwu_failed', round(time.monotonic() - start, 3))
