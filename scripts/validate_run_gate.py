"""Validate and optionally execute an action against a separately trusted user authority."""
import argparse
import json
from pathlib import Path
import sys

from openmd.core.run_gate import validate_gate
from openmd.runtime.interpreters import run_interruptibly


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', required=True, type=Path)
    parser.add_argument('--authority', required=True, type=Path)
    parser.add_argument('--action', required=True)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        validate_gate(json.loads(args.gate.read_text(encoding='utf-8')), json.loads(args.authority.read_text(encoding='utf-8')), action=args.action, target=args.target)
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    if args.command:
        command = args.command[1:] if args.command[0] == '--' else args.command
        return run_interruptibly(command, cwd=args.target, timeout=900).returncode
    print('{"ok":true}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
