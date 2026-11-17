"""Package the shipped extension without changing its manifest or fixed ID."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'src/openmd/browser_extension')
    parser.add_argument('--version', help='Package release version used in the ZIP filename')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    version = args.version or re.search(
        r'__version__ = "([^"]+)"', (ROOT / 'src/openmd/__init__.py').read_text(encoding='utf-8'),
    ).group(1)
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)', version):
        parser.error('Invalid release version')
    if not (args.source / 'manifest.json').is_file():
        parser.error('Extension source must contain manifest.json')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / f'openmd-extension-{version}.zip'
    with ZipFile(target, 'w', compression=ZIP_DEFLATED) as bundle:
        for path in sorted(args.source.rglob('*')):
            relative = path.relative_to(args.source)
            if path.is_file() and '__pycache__' not in relative.parts and path.suffix not in {'.py', '.pyc'}:
                bundle.write(path, relative.as_posix())
    print(target)


if __name__ == '__main__':
    main()
