"""Reject stale-tree or help-only evidence for a claim about usable content."""
import argparse
import hashlib
import json
from pathlib import Path

from openmd.runtime.interpreters import run_interruptibly


def validate(evidence: dict, tree: str) -> None:
    if evidence.get('schema') != 'openmd.delivery-evidence/v1' or evidence.get('tree') != tree:
        raise ValueError('stale_or_invalid_tree')
    if not isinstance(evidence.get('not_verified'), list) or not evidence.get('samples'):
        raise ValueError('claim_scope_missing')
    for sample in evidence['samples']:
        if not all(sample.get(key) for key in ('source', 'backend', 'version', 'readback', 'artifact', 'sha256')):
            raise ValueError('content_evidence_missing')
        path = Path(sample['artifact'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256'] or not path.read_text(encoding='utf-8-sig').strip():
            raise ValueError('artifact_drift_or_empty')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    tree = run_interruptibly(['git','rev-parse','HEAD^{tree}'],cwd=args.root,capture_output=True,text=True,timeout=10,check=True).stdout.strip()
    validate(json.loads(args.evidence.read_text(encoding='utf-8')),tree)
    print('{"ok":true}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
