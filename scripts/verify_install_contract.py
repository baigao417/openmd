"""Consume a built wheel outside the repository, offline, under narrow environments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile

from openmd.runtime.interpreters import run_interruptibly

REQUIRED = {'openmd/cli.py', 'openmd/cli_items.py', 'openmd/core/plugins.py', 'openmd/routing/declarations.json', 'openmd/routing/routes.json', 'openmd/routing/cards/builtin.json', 'openmd/runtime/opencli_adapters.py'}


def verify(wheel: Path, wheelhouse: Path | None = None) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        if not REQUIRED <= names or not any(n.endswith('/share/openmd/opencli/xiaoe/dispatch.js') for n in names):
            raise ValueError('wheel_required_file_missing')
    matrix = []
    with tempfile.TemporaryDirectory(prefix='openmd-install-contract-') as directory:
        root = Path(directory).resolve()
        environment = root / 'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        command = [str(python), '-m', 'pip', 'install', '--disable-pip-version-check', str(wheel.resolve())]
        if wheelhouse:
            command += ['--no-index', '--find-links', str(wheelhouse.resolve())]
        done = run_interruptibly(command, capture_output=True, timeout=300)
        if done.returncode:
            raise RuntimeError('wheel_install_failed')
        cwd = root / '中文 空格 cwd'
        cwd.mkdir()
        home = root / 'empty-home'
        home.mkdir()
        config = root / 'empty.json'
        config.write_text('{"schema":"openmd.local/v1"}', encoding='utf-8')
        for encoding in ('utf-8', 'cp936', 'cp1252'):
            output = cwd / encoding
            env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home), 'OPENMD_CONFIG': str(config),
                   'OPENMD_DATA_DIR': str(home / 'data'), 'OPENMD_RUNTIME_DIR': str(home / 'runtime'),
                   'OPENMD_AUTO_INSTALL_TOOLS': '0', 'PYTHONIOENCODING': encoding,
                   'PATH': os.pathsep.join([str(python.parent), str(Path(os.environ.get('SYSTEMROOT', '/usr')) / ('System32' if os.name == 'nt' else 'bin'))])}
            for key in ('PYTHONPATH', 'OPENMD_ASR_PYTHON', 'OPENMD_ROUTE_OVERLAY'):
                env.pop(key, None)
            probe = run_interruptibly([str(python), '-I', '-c', 'import openmd; print(openmd.__file__)'], cwd=cwd, env=env, capture_output=True, timeout=30)
            origin = probe.stdout.decode(encoding).strip()
            if probe.returncode or not Path(origin).is_relative_to(environment):
                raise ValueError('wheel_checkout_or_global_import')
            done = run_interruptibly([str(python), '-I', '-m', 'openmd', 'ingest', 'https://example.invalid/article', '--backend', 'mock', '--out', str(output)], cwd=cwd, env=env, capture_output=True, timeout=30)
            payload = json.loads(done.stdout.decode(encoding))
            artifacts = [Path(p) for p in payload.get('outputs', [])]
            if done.returncode or not payload.get('ok') or not artifacts or not all(p.is_relative_to(output) and 'Mock' in p.read_text(encoding='utf-8-sig') for p in artifacts):
                raise ValueError('wheel_final_artifact_contract_failed')
            # Verify adapter resources from the installed wheel, not the checkout.
            script = 'from openmd.runtime.opencli_adapters import install_xiaoe_adapters; p=install_xiaoe_adapters(); assert (p/"play-url.js").is_file()'
            done = run_interruptibly([str(python), '-I', '-c', script], cwd=cwd, env=env, capture_output=True, timeout=30)
            if done.returncode:
                raise ValueError('wheel_adapter_resources_missing')
            matrix.append({'encoding': encoding, 'outside_cwd': True, 'empty_config': True, 'narrow_path': True, 'installed_import': True, 'artifact_readback': True, 'adapter_install': True})
    return {'ok': True, 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'matrix': matrix,
            'real_network': False, 'backend': 'mock', 'scope': 'offline installed consumer contract; not live platform availability'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.wheel, args.wheelhouse)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        result = {'ok': False, 'error': str(error)}
    text = json.dumps(result, ensure_ascii=True, indent=2)+'\n'
    if args.report:
        args.report.write_text(text, encoding='utf-8')
    print(text)
    return not result['ok']


if __name__ == '__main__':
    sys.exit(main())
