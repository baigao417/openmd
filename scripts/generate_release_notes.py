"""Combine one CHANGELOG version with the shared update/download instructions."""
from __future__ import annotations

import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    section = re.search(
        rf'^## \[{re.escape(args.version)}\]\n(.*?)(?=^## |\Z)',
        changelog, re.MULTILINE | re.DOTALL,
    )
    if section is None or not section[1].strip():
        parser.error(f'CHANGELOG.md has no release notes for {args.version}')
    instructions = (ROOT / '.github/release-notes.md').read_text(encoding='utf-8')
    notes = section[1].strip() + '\n' + instructions.replace('VERSION', args.version)
    args.output.write_text(notes, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
