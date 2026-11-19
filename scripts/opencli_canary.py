"""Evaluate one exact npm version without changing the installed OpenCLI pin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from openmd.runtime.opencli_canary import main

if __name__ == '__main__':
    raise SystemExit(main())
