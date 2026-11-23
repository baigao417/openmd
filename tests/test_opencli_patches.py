import json
from pathlib import Path

import pytest


def test_patch_registry_covers_bridge_loader_and_dispatcher():
    from openmd.runtime.opencli_patches import PATCHES
    assert {'bridge', 'loader', 'dispatcher'} <= {p.kind for p in PATCHES}
    assert len({p.id for p in PATCHES}) == len(PATCHES)
    assert all(p.target and p.versions and p.description and p.verification for p in PATCHES)


def test_patch_drift_fails_with_patch_id():
    from openmd.runtime.opencli_patches import PATCHES, apply_patch
    patch = next(p for p in PATCHES if p.kind == 'loader')
    with pytest.raises(ValueError, match=patch.id):
        apply_patch('upstream has changed', patch, version='2.0.3')
    with pytest.raises(ValueError, match='version'):
        apply_patch(patch.original, patch, version='999.0.0')


def test_patch_matching_never_mutates_source(tmp_path):
    from openmd.runtime.opencli_patches import check_package_patches
    root = tmp_path / 'package'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({'version': '2.0.3'}), encoding='utf-8')
    status = check_package_patches(root)
    assert status['total'] > 0 and not status['ok']
    assert all(p['error_code'] for p in status['patches'] if not p['ok'])
    assert sorted(p.name for p in root.iterdir()) == ['package.json']


def test_doctor_exposes_patch_readiness(monkeypatch):
    from openmd.core.doctor import build_doctor_report
    from openmd.runtime import opencli_patches
    monkeypatch.setattr(opencli_patches, 'patch_diagnostics', lambda: {'total': 42, 'ok': False, 'patches': []})
    result = build_doctor_report('web')
    assert result['opencli_patches']['total'] == 42
    assert result['opencli_patches']['ok'] is False


def test_bilibili_subtitle_loader_patch_retries_player_v2_and_preserves_empty_error():
    from openmd.runtime.opencli_patches import PATCHES, apply_patch

    patch = next(p for p in PATCHES if p.id == 'bilibili-subtitle-player-v2-fallback')
    assert patch.kind == 'loader'
    assert patch.target == 'clis/bilibili/subtitle.js'
    assert patch.versions == ('2.0.3',)
    patched = apply_patch(patch.original, patch, version='2.0.3')
    assert 'need_login_subtitle' in patched
    assert "'/x/player/v2'" in patched
    assert 'attempt < 3' in patched
    assert '800' in patched
    assert 'throw new EmptyResultError' in patched


def test_bilibili_subtitle_loader_environment_is_registered():
    from openmd.runtime.opencli_patches import loader_environment

    env = loader_environment('bilibili', 'subtitle', base={'NODE_OPTIONS': '--trace-warnings'})
    assert '--import=data:text/javascript,' in env['NODE_OPTIONS']
    assert '--trace-warnings' in env['NODE_OPTIONS']
