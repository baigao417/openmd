"""Convert a reviewed feedback package into a loaded-chat fixture."""
from __future__ import annotations
import argparse, json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openmd.adapters.ai_chat import AiChatAdapter
from openmd.backends.ai_chat import ai_chat_backend_for_site
from openmd.backends.loaded_chat import extract_loaded_chat
from openmd.diagnostics.sanitize import sanitize_snapshot

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("package", type=Path); p.add_argument("--site", required=True); p.add_argument("--name", required=True); p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    html = (a.package / "structure.html").read_text(encoding="utf-8")
    url = json.loads((a.package / "manifest.json").read_text(encoding="utf-8")).get("source_url") or f"https://{a.site}/"
    item = AiChatAdapter("ai_chat", ai_chat_backend_for_site(a.site)).resolve_items(url)[0]
    result = extract_loaded_chat(html, item, backend="fixture")
    target = a.out / a.site; target.mkdir(parents=True, exist_ok=True)
    fixture = target / f"{a.name}.html"; expected = target / f"{a.name}.json"; test = target / f"test_{a.name}.py"
    fixture.write_text(html, encoding="utf-8")
    expected.write_text(json.dumps({"roles": [b.role for b in result.blocks], "turn_count": len(result.blocks), "status": result.status}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    test.write_text(f"from pathlib import Path\nfrom openmd.backends.loaded_chat import extract_loaded_chat\n\ndef test_{a.name}_fixture():\n    assert Path(__file__).with_name('{a.name}.html').is_file()\n", encoding="utf-8")
    print(json.dumps({"fixture": str(fixture), "expected": str(expected), "test": str(test)}, ensure_ascii=False))
    return 0
if __name__ == "__main__": raise SystemExit(main())
