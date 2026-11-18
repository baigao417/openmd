"""Install and evaluate one candidate without changing pins or shared hosts.

Run through the maintainer's heavy/browser lock. Reports deliberately retain
only command metadata, result counts and structured failure codes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlparse

from . import managed_tools
from .interpreters import run_interruptibly
from .opencli_inventory import ROOT, check_contracts, cli_definitions, inventory
from .opencli_patches import check_package_patches


def validate_version(version: str) -> str:
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', version):
        raise ValueError('opencli_canary_exact_version_required')
    return version


def install_candidate(version: str, staging: Path, *, timeout: int = 300) -> Path:
    node = managed_tools.node_dir()
    npm = managed_tools._npm_cli(node) if node else None
    if not node or not npm:
        raise ValueError('npm_runtime_missing')
    for name in ('user.npmrc', 'global.npmrc'):
        (staging / name).write_text('', encoding='utf-8')
    command = [str(node / ('node.exe' if os.name == 'nt' else 'node')), str(npm),
               'install', '--prefix', str(staging), '--cache', str(staging / 'npm-cache'),
               '--userconfig', str(staging / 'user.npmrc'), '--globalconfig', str(staging / 'global.npmrc'),
               f'{managed_tools.OPENCLI_PACKAGE}@{version}', '--ignore-scripts', '--no-audit',
               '--no-fund', '--save-exact', '--loglevel=error']
    if registry := os.environ.get('OPENMD_NPM_REGISTRY'):
        command += ['--registry', registry]
    env = {**{k: v for k, v in os.environ.items() if not k.upper().startswith(('NPM_CONFIG_', 'NPM_TOKEN', 'NODE_AUTH_TOKEN'))},
           'NODE_OPTIONS': '--max-old-space-size=1536',
           'PATH': str(node) + os.pathsep + os.environ.get('PATH', '')}
    result = run_interruptibly(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                               env=env, cwd=staging, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError(f'npm_install_failed:exit_{result.returncode}')  # no registry/session diagnostics
    package = staging / 'node_modules/@jackwener/opencli'
    if json.loads((package / 'package.json').read_text(encoding='utf-8')).get('version') != version:
        raise ValueError('npm_candidate_identity_mismatch')
    return package


def safe_probe(report: dict) -> dict:
    fields = ('platform', 'configured', 'status', 'mode', 'branch', 'error_code', 'extraction_status',
              'source_level', 'text_chars', 'item_count', 'route_version', 'tool_version', 'elapsed_seconds', 'live_route_attempted')
    result = {key: report.get(key) for key in ('completed', 'ok', 'configured_count', 'summary')}
    result['results'] = [{key: row.get(key) for key in fields if key in row} for row in report.get('results', [])]
    return result


def compare_probes(baseline: dict, candidate: dict) -> dict:
    before = {r['platform']: r for r in baseline.get('results', []) if r.get('configured')}
    after = {r['platform']: r for r in candidate.get('results', []) if r.get('configured')}
    changes = []
    for platform in sorted(set(before) | set(after)):
        a, b = before.get(platform, {}), after.get(platform, {})
        fields = ('status', 'branch', 'extraction_status', 'source_level', 'item_count', 'text_chars', 'error_code')
        changed = {key: {'before': a.get(key), 'after': b.get(key)} for key in fields if a.get(key) != b.get(key)}
        if changed:
            changes.append({'platform': platform, 'fields': changed})
    # A fallback change or output regression needs review even when both runs
    # return usable text. Elapsed times and growing live counts are informational.
    regressions = []
    for platform in before:
        a, b = before[platform], after.get(platform, {})
        if b.get('status') != 'ok' or a.get('branch') != b.get('branch') or a.get('source_level') != b.get('source_level'):
            regressions.append(platform)
        elif any(b.get(k, 0) < a.get(k, 0) for k in ('text_chars', 'item_count')):
            regressions.append(platform)
    ok = bool(before) and set(before) == set(after) and all(r.get('status') == 'ok' for r in before.values())
    ok = ok and bool(baseline.get('ok') and candidate.get('ok') and baseline.get('completed') and candidate.get('completed')) and not regressions
    return {'ok': ok, 'changes': changes, 'regressions': regressions}


def related_changes(candidate: Path, baseline: Path) -> list[dict]:
    changes = []
    for row in inventory()['commands']:
        if row['kind'] != 'upstream' or row['site'] == 'browser':
            continue
        relative = Path('clis') / row['site'] / (row['command'] + '.js')
        def metadata(root):
            try:
                return next(d for d in cli_definitions((root / relative).read_text(encoding='utf-8'))
                            if d['site'] == row['site'] and d['command'] == row['command'])
            except (OSError, ValueError, StopIteration):
                return None
        a, b = metadata(baseline), metadata(candidate)
        if a != b:
            changes.append({'command': row['site'] + '/' + row['command'], 'before': a, 'after': b})
    return changes


def _probe_config(explicit: Path | None) -> Path:
    from openmd.probe import default_config_path
    from openmd.core.local_config import local_config_path
    if explicit:
        return explicit
    path = default_config_path()
    location = local_config_path()
    return path if path.is_file() or not location else location.with_name('probe-config.json')


def _candidate_resolver() -> str:
    loader = """
import {pathToFileURL} from 'node:url';
import {resolve as resolvePathNative} from 'node:path';
const files = {'@jackwener/opencli/registry':'registry-api.js','@jackwener/opencli/errors':'errors.js'};
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@jackwener/opencli')) {
    if (!files[specifier]) throw new Error('opencli_canary_unknown_import');
    return {url:pathToFileURL(resolvePath(files[specifier])).href, shortCircuit:true};
  }
  return nextResolve(specifier, context);
}
function resolvePath(file) {return resolvePathNative(process.env.OPENMD_OPENCLI_CANARY_PACKAGE,'dist/src',file);}
"""
    url = 'data:text/javascript,' + quote(loader, safe='')
    bootstrap = "import {register} from 'node:module';register(" + json.dumps(url) + ',import.meta.url);'
    return '--import=data:text/javascript,' + quote(bootstrap, safe='')


def run_probe_pair(package: Path, staging: Path, *, config: Path | None, out: Path, timeout: int) -> tuple[dict, dict]:
    from openmd.probe import load_probe_samples
    from .services import daemon_status
    samples = load_probe_samples(_probe_config(config), required=True)
    samples = {key: value for key, value in samples.items() if urlparse(value.url).hostname != 'claude.ai'}
    if not samples:
        raise ValueError('probe_samples_not_configured')
    status = daemon_status()
    if not status or status.get('daemonVersion') != managed_tools.OPENCLI_VERSION:
        raise ValueError('canary_requires_existing_pinned_daemon')
    source_samples = staging / 'probe-config.json'
    from dataclasses import asdict
    source_samples.write_text(json.dumps({'schema': 'openmd.probe-config/v1', 'samples': {k: asdict(v) for k, v in samples.items()}}), encoding='utf-8')
    # Private URLs exist only in this automatically reclaimed task directory.
    config_dir = managed_tools.config_directory()
    receipts = []
    for label, host in (('baseline', managed_tools.managed_package_root()), ('candidate', package)):
        local = staging / (label + '-local.json')
        local.write_text(json.dumps({'schema': 'openmd.local/v1', 'tools': {'opencli': {
            'root': str(host.parents[2]), 'config_dir': str(config_dir)}}}), encoding='utf-8')
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'OPENMD_CONFIG': str(local),
               'OPENMD_DATA_DIR': str(staging / 'data'), 'OPENMD_AUTO_INSTALL_TOOLS': '0',
               'OPENMD_OPENCLI_CANARY': '1', 'OPENMD_OPENCLI_CANARY_PACKAGE': str(host),
               'NODE_OPTIONS': '--max-old-space-size=1536 ' + _candidate_resolver()}
        command = [sys.executable, '-m', 'openmd', 'probe', '--config', str(source_samples), '--out', str(staging / label),
                   '--enable-network-backends', '--enable-browser-backends', '--timeout', str(timeout), '--json']
        done = run_interruptibly(command, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env,
                                 timeout=len(samples) * (timeout + 10) + 60, check=False)
        try:
            receipt = json.loads(done.stdout)
            if receipt.get('schema') != 'openmd.probe/v1':
                raise ValueError('schema')
        except (ValueError, TypeError):
            raise ValueError(f'{label}_probe_invalid_receipt:exit_{done.returncode}') from None
        safe = safe_probe(receipt)
        (out / (label + '-probe.json')).write_text(json.dumps(safe, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        receipts.append(safe)
    return tuple(receipts)


def _write_report(out: Path, report: dict) -> None:
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = [f"# OpenCLI {report['candidate_version']} canary", '', f"Status: **{report['status']}**. Direct upgrade: **{str(report['direct_upgrade']).lower()}**.",
             '', 'This command reports only; the installed pin, user configuration and shared Node host are preserved.', '',
             f"- Baseline pin: {report['baseline_version']}",
             f"- Step: {report.get('step', 'finished')}",
             f"- Command contracts: {report.get('contracts', {}).get('ok', 'not_run')}",
             f"- Patch replay: {report.get('patches', {}).get('ok', 'not_run')} ({report.get('patches', {}).get('total', 0)} records)",
             f"- Live comparison: {report.get('comparison', {}).get('ok', 'not_run')}",
             f"- Related metadata changes: {len(report.get('related_changes', []))}", '', '## Findings', '']
    lines.extend('- ' + item for item in report.get('blockers', []))
    lines.extend('- ' + item for item in report.get('contracts', {}).get('failures', []))
    lines.extend('- ' + p['id'] + ': ' + p['error_code'] for p in report.get('patches', {}).get('patches', []) if not p['ok'])
    lines.extend('- Metadata changed: ' + item['command'] for item in report.get('related_changes', []))
    if report.get('comparison'):
        lines += ['', '| Sample platform | Baseline | Candidate |', '|---|---|---|']
        before = {r['platform']: r for r in report['baseline_probe']['results'] if r.get('configured')}
        after = {r['platform']: r for r in report['candidate_probe']['results'] if r.get('configured')}
        for platform in sorted(set(before) | set(after)):
            lines.append(f"| {platform} | {before.get(platform, {}).get('status', 'missing')} | {after.get(platform, {}).get('status', 'missing')} |")
    lines += ['', 'No source URLs, dialogue/message bodies, credentials or third-party stdout/stderr are included.',
              'A green run covers only the configured samples; extension reload and release approval remain joint acceptance.', '']
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def run_canary(version: str, *, out: Path, probe_config: Path | None = None, timeout: int = 90, install_timeout: int = 300) -> dict:
    validate_version(version)
    out.mkdir(parents=True, exist_ok=True)
    report = {'schema': 'openmd.opencli-canary/v1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'baseline_version': managed_tools.OPENCLI_VERSION, 'candidate_version': version,
              'status': 'RUNNING', 'direct_upgrade': False, 'blockers': [], 'pin_changed': False,
              'private_content_policy': 'metadata and numeric counters only'}
    try:
        with tempfile.TemporaryDirectory(prefix='openmd-canary-', dir=out) as directory:
            staging = Path(directory)
            report['step'] = 'install'
            _write_report(out, report)
            package = install_candidate(version, staging, timeout=install_timeout)
            report['step'] = 'contracts'
            _write_report(out, report)
            failures = check_contracts(package_root=package)
            report['contracts'] = {'ok': not failures, 'failures': failures}
            report['step'] = 'patches'
            _write_report(out, report)
            report['patches'] = check_package_patches(package)
            report['related_changes'] = related_changes(package, managed_tools.managed_package_root())
            if failures or not report['patches']['ok']:
                report['blockers'].append('candidate_static_contract_or_patch_mismatch; live probe was not started')
            else:
                report['step'] = 'live-probe'
                _write_report(out, report)
                baseline, candidate = run_probe_pair(package, staging, config=probe_config, out=out, timeout=timeout)
                report.update(baseline_probe=baseline, candidate_probe=candidate, comparison=compare_probes(baseline, candidate))
                report['direct_upgrade'] = report['comparison']['ok']
                report['status'] = 'PASS' if report['direct_upgrade'] else 'BLOCKED'
                if not report['direct_upgrade']:
                    report['blockers'].append('baseline_or_candidate_live_probe_not_green_or_output_regressed')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Exception text is used only for this module's own fixed structured codes.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[A-Za-z0-9_:;-]+', str(exc)) else type(exc).__name__
        report['blockers'].append(code)
    report['status'] = 'PASS' if report['direct_upgrade'] else 'BLOCKED'
    report['step'] = 'finished'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    _write_report(out, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/opencli-canary')
    parser.add_argument('--probe-config', type=Path)
    parser.add_argument('--timeout', type=int, choices=range(1, 301), default=90)
    parser.add_argument('--install-timeout', type=int, choices=range(1, 901), default=300)
    args = parser.parse_args(argv)
    try:
        report = run_canary(args.version, out=args.out, probe_config=args.probe_config, timeout=args.timeout, install_timeout=args.install_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({'status': report['status'], 'direct_upgrade': report['direct_upgrade'], 'report': str(args.out / 'report.md')}))
    return 0 if report['direct_upgrade'] else 1
