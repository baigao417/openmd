import json
import os
import subprocess
from pathlib import Path

import pytest


def test_canary_rejects_unpinned_npm_specs():
    from openmd.runtime.opencli_canary import validate_version
    for value in ['latest', '^2.0.3', '../local', '2.0.3 --global', 'https://example.org/pkg']:
        with pytest.raises(ValueError, match='version'):
            validate_version(value)
    assert validate_version('2.0.3') == '2.0.3'


def test_canary_comparison_requires_live_candidate_receipt():
    from openmd.runtime.opencli_canary import compare_probes
    baseline = {'completed': True, 'configured_count': 1, 'ok': True,
                'results': [{'platform': 'web_article', 'configured': True, 'status': 'ok', 'branch': 'fixture', 'text_chars': 40}]}
    assert compare_probes(baseline, baseline)['ok']
    broken = {**baseline, 'ok': False, 'results': [{**baseline['results'][0], 'status': 'broken'}]}
    assert not compare_probes(baseline, broken)['ok']
    assert not compare_probes(baseline, {'completed': True, 'configured_count': 0, 'ok': True, 'results': []})['ok']


def test_canary_process_never_repoints_shared_node_host(monkeypatch):
    from openmd.runtime import managed_tools
    monkeypatch.setenv('OPENMD_OPENCLI_CANARY', '1')
    monkeypatch.setattr(managed_tools, 'node_dir', lambda: (_ for _ in ()).throw(AssertionError('shared node host inspected')))
    managed_tools._prepare_bundled_node_host_package()


def test_canary_resolver_uses_candidate_exports_without_host_link(tmp_path):
    from openmd.runtime import managed_tools
    from openmd.runtime.opencli_canary import _candidate_resolver
    node = managed_tools.node_dir()
    if node is None:
        pytest.skip('Node is optional')
    package = tmp_path / 'candidate package'
    folder = package / 'dist/src'
    folder.mkdir(parents=True)
    (package / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
    (folder / 'registry-api.js').write_text("export const marker = 'candidate';", encoding='utf-8')
    env = {**os.environ, 'OPENMD_OPENCLI_CANARY_PACKAGE': str(package), 'NODE_OPTIONS': _candidate_resolver()}
    done = subprocess.run([str(node / ('node.exe' if os.name == 'nt' else 'node')), '--input-type=module',
                           '-e', "import {marker} from '@jackwener/opencli/registry'; console.log(marker)"],
                          env=env, capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'candidate'


def test_canary_environment_survives_isolated_platform_worker(monkeypatch):
    from openmd.runtime.interpreters import worker_environment
    monkeypatch.setenv('OPENMD_OPENCLI_CANARY', '1')
    monkeypatch.setenv('OPENMD_OPENCLI_CANARY_PACKAGE', 'synthetic-candidate')
    monkeypatch.setenv('NODE_OPTIONS', '--synthetic-test')
    result = worker_environment()
    assert result['OPENMD_OPENCLI_CANARY_PACKAGE'] == 'synthetic-candidate'
    assert result['NODE_OPTIONS'] == '--synthetic-test'


def test_canary_does_not_prepare_missing_config_directory(tmp_path, monkeypatch):
    from openmd.runtime import managed_tools
    config = tmp_path / 'missing-config'
    monkeypatch.setenv('OPENMD_OPENCLI_CANARY', '1')
    monkeypatch.setattr(managed_tools, 'config_directory', lambda: config)
    with pytest.raises(ValueError, match='config_not_prepared'):
        managed_tools.command_environment()
    assert not config.exists()


def test_candidate_location_config_preserves_other_tools_and_browser_policy(tmp_path):
    from openmd.runtime.opencli_canary import location_config_for_host
    original = {'schema': 'openmd.local/v1', 'tools': {
        'opencli': {'root': 'original-host'}, 'defuddle': 'existing-defuddle', 'x-tweet-fetcher': 'existing-reader'},
        'browser': {'window_strategy': 'existing-window'}, 'route_overlay': 'existing-overlay',
        'xiaoe_long_asr': {'enabled': True}}
    result = location_config_for_host(tmp_path / 'node_modules/@jackwener/opencli', tmp_path / 'config', original)
    assert result['tools']['defuddle'] == 'existing-defuddle'
    assert result['tools']['x-tweet-fetcher'] == 'existing-reader'
    assert result['browser'] == original['browser']
    assert result['route_overlay'] == original['route_overlay']
    assert original['tools']['opencli']['root'] == 'original-host'
    assert 'xiaoe_long_asr' not in result


def test_canary_receipts_drop_private_text_urls_and_diagnostics():
    from openmd.runtime.opencli_canary import safe_probe
    source = {'completed': True, 'ok': True, 'configured_count': 1,
              'results': [{'platform': 'ai_chat', 'configured': True, 'status': 'ok',
                           'text_chars': 123, 'text': 'PRIVATE_DIALOGUE', 'url': 'PRIVATE_URL',
                           'reason': 'PRIVATE_MESSAGE', 'attempts': [{'reason': 'PRIVATE_REPLY'}]}]}
    safe = json.dumps(safe_probe(source))
    assert 'PRIVATE_' not in safe and '123' in safe


def test_canary_never_runs_probes_after_patch_failure(tmp_path, monkeypatch):
    from openmd.runtime import opencli_canary as canary
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'package.json').write_text('{"version":"2.0.3"}', encoding='utf-8')
    monkeypatch.setattr(canary, 'install_candidate', lambda *a, **k: package)
    monkeypatch.setattr(canary, 'check_contracts', lambda **kw: [])
    monkeypatch.setattr(canary, 'check_package_patches', lambda *a, **k: {'ok': False, 'total': 1, 'patches': [{'id': 'fixture', 'ok': False, 'error_code': 'mismatch'}]})
    monkeypatch.setattr(canary, 'run_probe_pair', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unsafe probe')))
    report = canary.run_canary('2.0.3', out=tmp_path / 'report')
    assert report['status'] == 'BLOCKED' and not report['direct_upgrade']
    assert (tmp_path / 'report/report.md').is_file()
