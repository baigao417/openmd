"""One-command contributor setup for Windows, macOS and Linux.

    python scripts/dev_setup.py            # create .venv, install openmd-cli[dev], check tools
    python scripts/dev_setup.py --check    # only report what is missing
    python scripts/dev_setup.py --no-venv  # install into the current interpreter (CI, devcontainer)
    python scripts/dev_setup.py --hooks    # also run full pytest before every git push (opt-in)

Uses only the standard library so it runs before anything is installed.
It never installs system tools itself; it prints the right command per OS.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"

HINTS = {
    "ffmpeg": {"Windows": "winget install Gyan.FFmpeg", "Darwin": "brew install ffmpeg", "Linux": "sudo apt-get install ffmpeg  (或发行版对应包管理器)"},
    "node": {"Windows": "winget install OpenJS.NodeJS.LTS", "Darwin": "brew install node", "Linux": "见 https://nodejs.org 或 nvm"},
    "opencli": {"Windows": "npm install -g @jackwener/opencli", "Darwin": "npm install -g @jackwener/opencli", "Linux": "npm install -g @jackwener/opencli"},
}
PURPOSE = {
    "ffmpeg": "测试必需；音视频处理",
    "node": "可选；opencli 的运行时",
    "opencli": "可选；B 站字幕、公众号等浏览器路线，做这些平台时需要",
}


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if SYSTEM == "Windows" else "bin/python")


def run(command: list[str]) -> None:
    print("$ " + " ".join(command))
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        print()
        print("✗ 上一步失败。多半是网络问题：重试一次，或设置 PIP_INDEX_URL 使用 pip 镜像（如 https://pypi.tuna.tsinghua.edu.cn/simple）。")
        sys.exit(1)


def check_tools() -> int:
    print(f"\n系统：{SYSTEM} {platform.release()}  Python：{platform.python_version()}")
    if sys.version_info < (3, 10):
        print("  ✗ 需要 Python 3.10+")
        return 1
    missing_required = 0
    for tool in ("ffmpeg", "node", "opencli"):
        found = shutil.which(tool)
        mark = "✓" if found else ("✗" if tool == "ffmpeg" else "·")
        print(f"  {mark} {tool:<8} {PURPOSE[tool]}" + ("" if found else f"\n      → {HINTS[tool][SYSTEM if SYSTEM in HINTS[tool] else 'Linux']}"))
        missing_required += tool == "ffmpeg" and not found
    return 1 if missing_required else 0


def main() -> int:
    # This script runs before installation; the helper uses only stdlib.
    sys.path.insert(0, str(ROOT / "src"))
    from openmd.console import configure_console

    configure_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only report missing tools")
    parser.add_argument("--no-venv", action="store_true", help="install into the current interpreter")
    parser.add_argument("--hooks", action="store_true", help="opt in: git push runs the full test suite first")
    args = parser.parse_args()

    if not args.check:
        python = Path(sys.executable)
        if not args.no_venv:
            if not venv_python().exists():
                run([sys.executable, "-m", "venv", str(VENV)])
            python = venv_python()
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(python), "-m", "pip", "install", "-e", ".[dev]"])
        if args.hooks and (ROOT / ".git").exists():
            run(["git", "config", "core.hooksPath", "scripts/hooks"])

    status = check_tools()
    activate = r".venv\Scripts\activate" if SYSTEM == "Windows" else "source .venv/bin/activate"
    print("\n下一步：")
    if not args.no_venv:
        print(f"  {activate}")
    print("  python -m pytest              # 全量回归")
    print('  openmd "https://sspai.com/post/73145" -o out   # 真实样本冒烟')
    return status


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
