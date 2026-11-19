"""Vendor the OpenCLI Browser Bridge into the Openmd extension (one extension for users).

    python scripts/vendor_opencli_bridge.py            # fetch pinned release, verify, patch, write
    python scripts/vendor_opencli_bridge.py --check    # verify the vendored copy matches this script

Source: the official release asset of jackwener/opencli (Apache-2.0). Bump the
three pins below together with OPENCLI_VERSION in src/openmd/runtime/managed_tools.py.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from openmd.runtime.opencli_patches import (
    BRIDGE_PATCHES as PATCHES, OPENCLI_TAG, BRIDGE_VERSION, BRIDGE_SHA256, patch_bridge,
)

LICENSE_SHA256 = "0210b8b66cf00358242cb921ba2be3a46dfe0190159b1b952388a3880ce1ff54"
LICENSE_URL = f"https://raw.githubusercontent.com/jackwener/opencli/{OPENCLI_TAG}/LICENSE"
ZIP_URL = f"https://github.com/jackwener/opencli/releases/download/{OPENCLI_TAG}/opencli-extension-v{BRIDGE_VERSION}.zip"

EXTENSION = ROOT / "src" / "openmd" / "browser_extension"
VENDOR = EXTENSION / "vendor" / "opencli-bridge"

# Openmd's own idle page (upstream idle.html is replaced, not patched).
IDLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Openmd</title>
<style>body{font:15px/1.6 system-ui,sans-serif;color:#555;background:#fafafa;display:grid;place-items:center;height:100vh;margin:0}</style></head>
<body><p>这是 Openmd 的后台读取窗口，可以忽略；关掉也不影响使用。</p></body>
</html>
"""
HEADER = (
    "// OpenCLI Browser Bridge {version} (Apache-2.0, https://github.com/jackwener/opencli),\n"
    "// vendored by scripts/vendor_opencli_bridge.py. Modified by Openmd: see VENDOR.md.\n"
    'const OPENMD_BRIDGE_VERSION = "{version}";\n'
)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "openmd-vendor"})
    try:
        response = urllib.request.urlopen(request, timeout=30)  # noqa: S310 - fixed https URLs
    except urllib.error.URLError:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30)
    with response:
        return response.read()


def build(archive_path: Path | None = None, license_path: Path | None = None) -> dict[str, bytes]:
    archive = archive_path.read_bytes() if archive_path else fetch(ZIP_URL)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != BRIDGE_SHA256:
        raise SystemExit(f"sha256 mismatch for {ZIP_URL}: {digest}")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        background = bundle.read("dist/background.js").decode("utf-8")
        bundle.read("idle.html")  # must still exist upstream: the bridge navigates to it
    try:
        background = patch_bridge(background)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    background = HEADER.format(version=BRIDGE_VERSION) + background
    patches = "\n".join(f"- {description}: `{original}` → `{replacement}`" for description, original, replacement, _ in PATCHES)
    vendor_md = (
        f"# OpenCLI Browser Bridge (vendored)\n\n"
        f"- Upstream: https://github.com/jackwener/opencli (Apache-2.0), release {OPENCLI_TAG}, "
        f"extension {BRIDGE_VERSION}\n- Asset: {ZIP_URL}\n- SHA-256: `{BRIDGE_SHA256}`\n"
        f"- Files: `dist/background.js` → `background.js`; upstream `idle.html` replaced by Openmd's own page at the extension root\n\n"
        f"## Modifications by Openmd\n\n{patches}\n\n"
        f"Regenerate with `python scripts/vendor_opencli_bridge.py`; never edit `background.js` by hand.\n"
    )
    license_bytes = license_path.read_bytes() if license_path else fetch(LICENSE_URL)
    if hashlib.sha256(license_bytes).hexdigest() != LICENSE_SHA256:
        raise SystemExit("license sha256 mismatch")
    return {
        "background.js": background.encode("utf-8"),
        "LICENSE": license_bytes,
        "VENDOR.md": vendor_md.encode("utf-8"),
        "../../idle.html": IDLE_HTML.encode("utf-8"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare the vendored files with a fresh build")
    parser.add_argument("--archive", type=Path, help="local official release archive, checked against the pinned SHA-256")
    parser.add_argument("--license-file", type=Path, help="local license, checked against the pinned SHA-256")
    args = parser.parse_args()
    files = build(args.archive, args.license_file)
    VENDOR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, data in files.items():
        target = (VENDOR / name).resolve()
        if args.check:
            current = target.read_bytes().replace(b"\r\n", b"\n") if target.is_file() else None
            if current != data.replace(b"\r\n", b"\n"):
                stale.append(str(target.relative_to(ROOT)))
        else:
            target.write_bytes(data)
    if stale:
        print("vendored bridge is stale:", ", ".join(stale))
        return 1
    print("vendored bridge is up to date" if args.check else f"wrote OpenCLI Browser Bridge {BRIDGE_VERSION} into {VENDOR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
