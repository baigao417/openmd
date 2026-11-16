"""Generate route views from the same declarations used by runtime selection."""

from __future__ import annotations

import argparse
import json

from .loader import route_view


def doctor_tools(*, probe_commands: bool = False):
    from .providers import doctor_tools as build_report

    return build_report(probe_commands=probe_commands)


def render_routes_markdown(*, source: str | None = None, effective: bool = False) -> str:
    view = route_view(source, effective=effective, include_plugins=effective)
    lines = ["# Openmd routes", "", f"Generated from bundled cards and routes, version `{view['version']}`.", "", "| Source | Capability | Ordered backends |", "|---|---|---|"]
    for route in view["routes"]:
        lines.append(f"| {route['source']} | {route['capability']} | {' → '.join(route['chain'])} |")
    lines += ["", "Tools run only with the gates declared in their cards. Media steps require local ASR before producing text.", "", "Local overlay applied: " + str(view["overlay_applied"]).lower() + ".", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--format", choices=("json", "table", "markdown"), default="table")
    parser.add_argument("--bundled", action="store_true", help="Use bundled routes only when generating repository documentation.")
    args = parser.parse_args(argv)
    if args.format == "json":
        print(json.dumps(route_view(args.source, effective=not args.bundled, include_plugins=not args.bundled), ensure_ascii=False, indent=2))
    else:
        print(render_routes_markdown(source=args.source, effective=not args.bundled), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
