"""Bounded startup probe, requiring an actual thread.started session id."""
import argparse
import json
import subprocess

from openmd.runtime.interpreters import owned_popen, terminate_quietly
from openmd.runtime.startup_receipt import wait_for_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0]=='--' else args.command
    if not command:
        parser.error('supply a startup probe command after --')
    process = owned_popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    try:
        result = wait_for_session(process,timeout=args.timeout)
        print(json.dumps(result))
        return 0
    except (ValueError,TimeoutError) as error:
        print(json.dumps({'ok':False,'error':str(error)}))
        return 1
    finally:
        terminate_quietly(process)


if __name__=='__main__':
    raise SystemExit(main())
