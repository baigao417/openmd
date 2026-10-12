"""Safe OpenCLI command wrapper scaffold."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .base import BackendCommandResult


class OpenCliBackend:
    """Run read-only OpenCLI commands without persisting raw stdout or stderr."""

    def __init__(self, executable: str = "opencli", *, timeout: int = 60) -> None:
        self.executable = executable
        self.timeout = timeout

    def run(self, args: Sequence[str], *, name: str = "opencli") -> BackendCommandResult:
        command = (self.executable, *args)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            return BackendCommandResult(
                name=name,
                command=command,
                exit_code=127,
                error_summary=sanitize_error_summary(exc),
            )
        except subprocess.TimeoutExpired as exc:
            return BackendCommandResult(
                name=name,
                command=command,
                exit_code=124,
                stdout_summary=sanitize_error_summary(exc.stdout or "") or "",
                stderr_summary=sanitize_error_summary(exc.stderr or "") or "",
                error_summary=f"command timed out after {self.timeout}s",
            )

        stderr_summary = sanitize_error_summary(completed.stderr or "") or ""
        stdout_summary = sanitize_error_summary(completed.stdout or "") or ""
        return BackendCommandResult(
            name=name,
            command=command,
            exit_code=completed.returncode,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            error_summary=stderr_summary if completed.returncode else None,
        )


def command_result_to_transcript(
    result: BackendCommandResult,
    *,
    source_level: str = "third_party_extractor",
) -> TranscriptResult:
    if result.success and result.stdout_summary.strip():
        return TranscriptResult(
            text=result.stdout_summary,
            status="verified",
            source_level=source_level,  # type: ignore[arg-type]
            backend=result.name,
            source_notes=["OpenCLI wrapper returned summarized stdout"],
        )
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=result.name,
        error_code=f"backend_exit_{result.exit_code}",
        error_summary=result.error_summary or result.stderr_summary or "OpenCLI command returned no usable text.",
    )
