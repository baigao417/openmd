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
import zipfile
from pathlib import Path

OPENCLI_TAG = "v2.0.3"
BRIDGE_VERSION = "1.0.27"
BRIDGE_SHA256 = "8636d42a0fe44d7ba3b0603dbcd9581e8269ccbca950136d0dfc0ffc39a8a213"
LICENSE_URL = f"https://raw.githubusercontent.com/jackwener/opencli/{OPENCLI_TAG}/LICENSE"
ZIP_URL = f"https://github.com/jackwener/opencli/releases/download/{OPENCLI_TAG}/opencli-extension-v{BRIDGE_VERSION}.zip"

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "openmd" / "browser_extension"
VENDOR = EXTENSION / "vendor" / "opencli-bridge"

# (description, original, replacement): each original must occur exactly as counted.
PATCHES = [
    (
        "fixed profile id so Openmd can select this bridge even when the standalone OpenCLI extension is also connected",
        "const generated = generateContextId();",
        'const generated = "openmd"; // Openmd patch: fixed Browser Bridge profile id',
        1,
    ),
    (
        "report the bridge's own version, not the host Openmd extension's manifest version",
        "chrome.runtime.getManifest().version",
        "OPENMD_BRIDGE_VERSION",
        2,
    ),
    (
        "background work minimizes its window right after creating it",
        """  const win = await chrome.windows.create({
    url: startUrl,
    focused: mode === "foreground",
    width: 1280,
    height: 900,
    type: "normal"
  });""",
        """  const win = await chrome.windows.create({
    url: startUrl,
    focused: mode === "foreground",
    width: 1280,
    height: 900,
    type: "normal"
  });
  // Openmd patch: background reads never stay on screen. windows.create ignores
  // state:"minimized" on Windows (Edge/Chrome 157), so minimize right after.
  if (mode !== "foreground") await chrome.windows.update(win.id, { state: "minimized" }).catch(() => {});""",
        1,
    ),
    (
        "tab group names show Openmd",
        'interactive: "OpenCLI Browser",',
        'interactive: "Openmd",',
        1,
    ),
    (
        "tab group names show Openmd (automation)",
        'automation: "OpenCLI Adapter"',
        'automation: "Openmd"',
        1,
    ),
]

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
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs
        return response.read()


def build() -> dict[str, bytes]:
    archive = fetch(ZIP_URL)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != BRIDGE_SHA256:
        raise SystemExit(f"sha256 mismatch for {ZIP_URL}: {digest}")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        background = bundle.read("dist/background.js").decode("utf-8")
        bundle.read("idle.html")  # must still exist upstream: the bridge navigates to it
    for description, original, replacement, count in PATCHES:
        found = background.count(original)
        if found != count:
            raise SystemExit(f"patch no longer applies ({description}): expected {count}, found {found}")
        background = background.replace(original, replacement)
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
    return {
        "background.js": background.encode("utf-8"),
        "LICENSE": fetch(LICENSE_URL),
        "VENDOR.md": vendor_md.encode("utf-8"),
        "../../idle.html": IDLE_HTML.encode("utf-8"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare the vendored files with a fresh build")
    args = parser.parse_args()
    files = build()
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
