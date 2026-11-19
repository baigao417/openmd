"""Generate Openmd's static OpenCLI inventory or check a managed package offline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from openmd.runtime.opencli_inventory import check_contracts, projections


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--contracts', action='store_true')
    parser.add_argument('--package-root', type=Path)
    args = parser.parse_args(argv)
    if args.contracts:
        failures = check_contracts(package_root=args.package_root)
    else:
        outputs = projections(ROOT)
        failures = [str(p.relative_to(ROOT)) for p, value in outputs.items() if not p.is_file() or p.read_text(encoding='utf-8') != value] if args.check else []
        if not args.check:
            for path, value in outputs.items():
                path.write_text(value, encoding='utf-8', newline='\n')
    print(json.dumps({'ok': not failures, 'failures': failures}))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
