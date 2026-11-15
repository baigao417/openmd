# OpenCLI Browser Bridge (vendored)

- Upstream: https://github.com/jackwener/opencli (Apache-2.0), release v2.0.3, extension 1.0.27
- Asset: https://github.com/jackwener/opencli/releases/download/v2.0.3/opencli-extension-v1.0.27.zip
- SHA-256: `8636d42a0fe44d7ba3b0603dbcd9581e8269ccbca950136d0dfc0ffc39a8a213`
- Files: `dist/background.js` → `background.js`; upstream `idle.html` replaced by Openmd's own page at the extension root

## Modifications by Openmd

- fixed profile id so Openmd can select this bridge even when the standalone OpenCLI extension is also connected: `const generated = generateContextId();` → `const generated = "openmd"; // Openmd patch: fixed Browser Bridge profile id`
- report the bridge's own version, not the host Openmd extension's manifest version: `chrome.runtime.getManifest().version` → `OPENMD_BRIDGE_VERSION`
- background work minimizes its window right after creating it: `  const win = await chrome.windows.create({
    url: startUrl,
    focused: mode === "foreground",
    width: 1280,
    height: 900,
    type: "normal"
  });` → `  const win = await chrome.windows.create({
    url: startUrl,
    focused: mode === "foreground",
    width: 1280,
    height: 900,
    type: "normal"
  });
  // Openmd patch: background reads never stay on screen. windows.create ignores
  // state:"minimized" on Windows (Edge/Chrome 157), so minimize right after.
  if (mode !== "foreground") await chrome.windows.update(win.id, { state: "minimized" }).catch(() => {});`
- tab group names show Openmd: `interactive: "OpenCLI Browser",` → `interactive: "Openmd",`
- tab group names show Openmd (automation): `automation: "OpenCLI Adapter"` → `automation: "Openmd"`

Regenerate with `python scripts/vendor_opencli_bridge.py`; never edit `background.js` by hand.
