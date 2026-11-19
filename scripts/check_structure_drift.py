"""Compare structural signatures from a fixture and a feedback package."""
from __future__ import annotations
import argparse, json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openmd.diagnostics.sanitize import sanitize_snapshot

def _sig(path, url):
    return sanitize_snapshot(Path(path).read_text(encoding="utf-8"), url).manifest["structure_hash"]
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("old", type=Path); p.add_argument("new", type=Path); p.add_argument("--url", required=True); a=p.parse_args(argv)
    old=_sig(a.old,a.url); new=json.loads((a.new/"manifest.json").read_text(encoding="utf-8"))["structure_hash"]
    payload={"drift": old != new, "old_structure_hash": old, "new_structure_hash": new, "site": a.url}
    print(json.dumps(payload, ensure_ascii=False, indent=2)); return 1 if payload["drift"] else 0
if __name__ == "__main__": raise SystemExit(main())
