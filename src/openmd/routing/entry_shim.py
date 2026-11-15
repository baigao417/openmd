"""Execute isolated Python entry points with credential and TLS boundaries."""

from __future__ import annotations

import argparse
import importlib
import runpy
import sys
from pathlib import Path


def _require_tls_verification() -> None:
    try:
        import requests
    except ImportError:
        return
    original = requests.sessions.Session.send

    def verified_send(self, request, **kwargs):
        # Some downloaders set Session.verify=False internally. Never inherit it.
        kwargs["verify"] = True
        return original(self, request, **kwargs)

    requests.sessions.Session.send = verified_send


def _require_yutto_tls_verification() -> None:
    """Yutto's native session is independent of requests and defaults to insecure."""
    fetcher = importlib.import_module("yutto.utils.fetcher")
    original = fetcher.create_client

    def verified_client(*args, **kwargs):
        kwargs["verify"] = True
        return original(*args, **kwargs)

    fetcher.create_client = verified_client
    # Cover aliases imported before shim initialization as well as future imports.
    for name, module in tuple(sys.modules.items()):
        if name.startswith("yutto.") and getattr(module, "create_client", None) is original:
            module.create_client = verified_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--entry")
    parser.add_argument("--script")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    for path in reversed(args.path):
        sys.path.insert(0, str(Path(path).resolve()))
    if bool(args.entry) == bool(args.script):
        raise ValueError("entry_shim_requires_one_entry")
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    _require_tls_verification()
    if args.entry and args.entry.split(":", 1)[0].split(".", 1)[0] == "yutto":
        _require_yutto_tls_verification()
    if args.entry:
        module, name = args.entry.split(":", 1)
        sys.argv = [module, *arguments]
        result = getattr(importlib.import_module(module), name)()
        return result if isinstance(result, int) else 0
    script = Path(args.script).resolve()
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *arguments]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
