"""Offline envelope replay and actual installed OpenCLI dispatcher sentinel."""
import argparse
import json
import os
from pathlib import Path

from openmd.core.opencli_output import load_opencli_json
from openmd.runtime.interpreters import run_interruptibly
from openmd.runtime.managed_tools import managed_package_root, node_dir, OPENCLI_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', required=True)
    parser.add_argument('--host', type=Path, default=managed_package_root())
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    rows = [{'id': 'fixture', 'duration_ms': 1234}]
    for value in (rows, {'ok':True,'data':rows}):
        assert load_opencli_json(json.dumps(value)) == rows
    failure = {'ok':False, 'error':{'name':'SessionExpired'}}
    assert load_opencli_json(json.dumps(failure)) == failure
    assert json.loads((args.host/'package.json').read_text(encoding='utf-8'))['version'] == OPENCLI_VERSION
    node = node_dir()
    if node is None:
        raise RuntimeError('node_missing')
    done = run_interruptibly([str(node/('node.exe' if os.name=='nt' else 'node')), str(root/'tests/fixtures/contracts/dispatch-sentinel.mjs'), str(args.host)], env={**os.environ,'NODE_OPTIONS':'--max-old-space-size=1536'}, capture_output=True, text=True, encoding='utf-8', timeout=30)
    result = {'ok':done.returncode == 0, 'envelope':True, 'dispatcher':json.loads(done.stdout) if done.returncode == 0 else False,
              'host_version':OPENCLI_VERSION,'network':False}
    if args.report:
        args.report.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))
    return not result['ok']


if __name__=='__main__':
    raise SystemExit(main())
