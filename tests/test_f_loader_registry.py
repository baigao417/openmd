"""One audited source for process-local AI loader patches."""
from pathlib import Path

import pytest


@pytest.mark.parametrize('site', ['grok', 'chatgpt', 'deepseek', 'doubao', 'gemini', 'qwen', 'yuanbao'])
def test_ai_loaders_are_registered_for_each_read_operation(site):
    from openmd.runtime.opencli_patches import PATCHES
    for operation in ('detail', 'history', 'status', 'read'):
        patches = [p for p in PATCHES if p.kind == 'loader' and p.target == f'clis/{site}/{operation}.js']
        assert patches, (site, operation)
        assert all(p.original and p.count == 1 and p.versions == ('2.0.3',) for p in patches)
        assert any('openmdBackgroundPage' in p.replacement for p in patches)


def test_ai_chat_loading_is_only_a_registry_adapter():
    from openmd.runtime import ai_chat_loading
    source = Path(ai_chat_loading.__file__).read_text(encoding='utf-8')
    assert 'loader_environment(' in source
    for duplicate in ('function openmd', 'const POLL_DEADLINE_MS', 'export async function load', 'source.replace', 'func: async'):
        assert duplicate not in source


def test_unified_inventory_covers_f_providers_and_status_call():
    from openmd.runtime.opencli_inventory import inventory
    commands = {(row['site'], row['command']) for row in inventory()['commands']}
    assert {(site, 'detail') for site in ('chatgpt', 'deepseek', 'grok', 'doubao', 'gemini', 'qwen', 'yuanbao')} <= commands
    assert {(site, 'status') for site in ('chatgpt', 'doubao', 'gemini', 'qwen', 'yuanbao')} <= commands


@pytest.mark.parametrize('failure', ['helpers', 'handler', 'measurement', 'stability-2', 'duplicate', 'version', 'missing-version'])
def test_loader_fails_closed_for_every_injection_boundary(tmp_path, pinned_ai_package, failure):
    import json
    import shutil
    import subprocess

    from openmd.runtime.opencli_patches import PATCHES, loader_environment
    node = shutil.which('node')
    if not node:
        pytest.skip('Node not installed')
    patches = [p for p in PATCHES if p.target == 'clis/grok/detail.js']
    # All real marker boundaries, without site content or browser calls.
    source = "import { cli, Strategy } from '@jackwener/opencli/registry';\ncli({\n"
    source += "    func: async (page, kwargs) => {\nlet bubbles = [];\nconst POLL_DEADLINE_MS = 20_000;\n"
    source += "if (bubbles.length > 0) break;\nif (!bubbles.length) return [];\n    },\n});"
    # break only needs to be syntactically valid after the loader checks.
    source = source.replace('if (bubbles.length > 0) break;', 'while (false) { if (bubbles.length > 0) break; }')
    if failure in {'version', 'missing-version'}:
        version = {'type': 'module', **({'version': '9.0.0'} if failure == 'version' else {})}
        (tmp_path / 'package.json').write_text(json.dumps(version), encoding='utf-8')
        expected = 'opencli_patch_version_incompatible:grok-detail-helpers'
    else:
        label = 'handler' if failure == 'duplicate' else failure
        patch = next(p for p in patches if p.id == 'grok-detail-' + label)
        source = source + '\n//' + patch.original if failure == 'duplicate' else source.replace(patch.original, '/* source drift */')
        expected = 'opencli_patch_mismatch:' + patch.id
    module = tmp_path / 'clis/grok/detail.js'
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding='utf-8')
    done = subprocess.run([node, '--input-type=module', '-e', f'import({json.dumps(module.as_uri())})'],
                          env=loader_environment('grok'), capture_output=True, text=True, timeout=10)
    assert done.returncode != 0
    assert expected in done.stderr


def test_loader_keeps_e_base_environment_api_and_limits_selected_module():
    from urllib.parse import unquote
    from openmd.runtime.opencli_patches import loader_environment
    base = {'NODE_OPTIONS': '--trace-warnings', 'SYNTHETIC': 'preserved'}
    env = loader_environment('gemini', 'history', base=base)
    assert env['SYNTHETIC'] == 'preserved'
    assert env['NODE_OPTIONS'].startswith('--trace-warnings ')
    assert base == {'NODE_OPTIONS': '--trace-warnings', 'SYNTHETIC': 'preserved'}
    loader = unquote(unquote(env['NODE_OPTIONS']))
    assert '/clis/gemini/history.js' in loader
    assert '/clis/gemini/detail.js' not in loader
    with pytest.raises(ValueError, match='opencli_patch_loader_not_registered'):
        loader_environment('gemini', 'send')
