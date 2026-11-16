"""Generate public projections from declarations.json; never read local overlays."""
from __future__ import annotations

import argparse
import json
from importlib.resources import files
from pathlib import Path

from .view import render_route_table


def declarations() -> dict:
    return json.loads(files('openmd.routing').joinpath('declarations.json').read_text(encoding='utf-8'))


def projections(root: Path) -> dict[Path, str]:
    declaration = declarations()
    outputs = {root / 'src/openmd/routing/routes.json': declaration['routes']}
    outputs.update({root / 'src/openmd/routing/cards' / name: card for name, card in declaration['cards'].items()})
    result = {path: json.dumps(value, ensure_ascii=False, indent=2) + '\n' for path, value in outputs.items()}
    routes = declaration['routes']
    result[root / 'docs/routes.generated.md'] = render_route_table({**routes, 'overlay_applied': False})
    return result


def check(root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path, expected in projections(root).items() if not path.is_file() or path.read_text(encoding='utf-8') != expected]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if args.check:
        drift = check(args.root)
        print(json.dumps({'ok': not drift, 'drift': drift}))
        return bool(drift)
    for path, text in projections(args.root).items():
        path.write_text(text, encoding='utf-8', newline='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
