import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("exit_code", [0, 7])
def test_pre_push_propagates_full_pytest_exit_status(tmp_path, exit_code):
    root = Path(__file__).resolve().parents[1]
    bash = "E:/Git/Git/bin/bash.exe" if os.name == "nt" else shutil.which("sh")
    if not bash or not Path(bash).is_file():
        pytest.skip("Git shell unavailable")
    # Test the hook without pushing or running a second suite. The stub records
    # the exact Python arguments, then deliberately succeeds/fails.
    stub = tmp_path / "python"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path.as_posix()}/args.txt"\nexit {exit_code}\n', encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([bash, "scripts/hooks/pre-push"], cwd=root, env=env, capture_output=True, timeout=10)
    assert result.returncode == exit_code, result.stderr
    assert (tmp_path / "args.txt").read_text().splitlines() == ["-m", "pytest"]
