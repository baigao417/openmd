"""Cloud opt-in, independent duration and fail-closed Tingwu contracts."""
import json
from types import SimpleNamespace

import pytest

from openmd.core.models import TranscriptResult


@pytest.mark.parametrize('duration,enabled,cloud,expected,reason', [
    (600, False, False, 'asr.auto', 'cloud_not_authorized'),
    (600, True, False, 'asr.tingwu', 'local_long_course_opt_in'),
    (599.9, True, False, 'asr.auto', 'below_threshold'),
    (None, True, False, 'asr.auto', 'duration_unknown'),
    (600, False, True, 'asr.tingwu', 'explicit_cloud_gate'),
])
def test_selection(duration, enabled, cloud, expected, reason):
    from openmd.runtime.tingwu import select_xiaoe_asr
    selected = select_xiaoe_asr(duration, {'enabled': enabled}, allow_cloud=cloud)
    assert selected['selected_backend'] == expected
    assert selected['reason'] == reason
    assert selected['threshold_seconds'] == 600


@pytest.mark.parametrize('setting', [None, True, {}, {'enabled': 'yes'}, {'enabled': True, 'threshold_seconds': 0}, {'enabled': True, 'threshold_seconds': float('nan')}, {'enabled': True, 'unknown': 1}])
def test_invalid_cloud_configuration(tmp_path, monkeypatch, setting):
    from openmd.core.local_config import load_local_config
    path = tmp_path / 'local.json'
    path.write_text(json.dumps({'schema': 'openmd.local/v1', 'xiaoe_long_asr': setting}), encoding='utf-8')
    monkeypatch.setenv('OPENMD_CONFIG', str(path))
    with pytest.raises(ValueError, match='xiaoe_long_asr_invalid'):
        load_local_config()


def test_cloud_gate_prevents_dispatch_and_no_fallback(tmp_path):
    from openmd.runtime.tingwu import TingwuASRBackend
    calls = []
    backend = TingwuASRBackend(allow_cloud=False, runner=lambda *a, **k: calls.append(a))
    assert backend.transcribe(tmp_path / 'audio.mp3').error_code == 'cloud_not_authorized'
    assert calls == []


def test_tingwu_receipt_is_allowlisted_and_failure_is_explicit(tmp_path):
    from openmd.runtime.tingwu import TingwuASRBackend
    audio = tmp_path / 'audio.mp3'
    audio.write_bytes(b'fake audio')
    def runner(*args, **kwargs):
        return 1, json.dumps({'ok': False, 'error_code': 'tingwu_auth_required', 'secret_extra': 'unsafe'}), 'https://example.invalid/?sign=private'
    result = TingwuASRBackend(allow_cloud=True, runner=runner, duration_reader=lambda p: 700).transcribe(audio)
    assert result.error_code == 'tingwu_auth_required'
    assert 'private' not in repr(result) and 'unsafe' not in repr(result)
    assert result.backend == 'asr.tingwu'


def test_pipeline_uses_downloaded_duration_and_records_selection(monkeypatch):
    from openmd.backends.xiaoe import OpenCliXiaoeTranscriptProvider
    from test_xiaoe_media import request
    monkeypatch.setattr('openmd.core.local_config.load_local_config', lambda: {'xiaoe_long_asr': {'enabled': True}})
    monkeypatch.setattr('openmd.runtime.tingwu.probe_audio_duration', lambda p: 5.015)
    def runner(*a, **k):
        return 0, json.dumps([{'resource_id': 'v_ab12', 'm3u8_url': 'https://media.example/a.m3u8', 'duration_sec': 12503}]), ''
    local = SimpleNamespace(transcribe=lambda p: TranscriptResult(text='[00:00] 短音频', status='verified', source_level='media_asr', backend='asr.auto'))
    provider = OpenCliXiaoeTranscriptProvider(runner=runner, allow_local_asr=True, allow_media_download=True,
        local_asr=local, downloader=lambda url, p, **k: p.write_bytes(b'audio'))
    result = provider.extract(request())
    assert result.metadata['asr_selection']['reason'] == 'below_threshold'
    assert result.metadata['asr_selection']['duration_seconds'] == 5.015


def test_selection_survives_runlog_sanitization():
    from openmd.core.secrets import sanitize_asr_execution
    raw = {'asr_backend': 'asr.tingwu', 'asr_selection': {'selected_backend': 'asr.tingwu', 'reason': 'local_long_course_opt_in', 'duration_seconds': 700, 'threshold_seconds': 600, 'unexpected': 'unsafe'}}
    result = sanitize_asr_execution(raw)
    assert result['asr_backend'] == 'asr.tingwu'
    assert result['asr_selection'] == {k: v for k, v in raw['asr_selection'].items() if k != 'unexpected'}


def test_custom_threshold_is_inclusive():
    from openmd.runtime.tingwu import select_xiaoe_asr
    setting = {'enabled': True, 'threshold_seconds': 42}
    assert select_xiaoe_asr(42, setting)['selected_backend'] == 'asr.tingwu'
    assert select_xiaoe_asr(41.9, setting)['selected_backend'] == 'asr.auto'


def test_tingwu_failure_never_runs_local_fallback(monkeypatch):
    from openmd.backends.xiaoe import OpenCliXiaoeTranscriptProvider
    from test_xiaoe_media import request
    monkeypatch.setattr('openmd.core.local_config.load_local_config', lambda: {'xiaoe_long_asr': {'enabled': True}})
    monkeypatch.setattr('openmd.runtime.tingwu.probe_audio_duration', lambda p: 700)
    monkeypatch.setattr('openmd.runtime.tingwu.TingwuASRBackend.transcribe', lambda self, p: TranscriptResult(text='',status='failed',source_level='partial_extraction',backend='asr.tingwu',error_code='tingwu_auth_required'))
    local = SimpleNamespace(transcribe=lambda p: pytest.fail('unexpected local fallback'))
    provider = OpenCliXiaoeTranscriptProvider(runner=lambda *a, **k: (0,json.dumps([{'resource_id':'v_ab12','m3u8_url':'https://example.invalid/a.m3u8'}]),''),
        allow_local_asr=True,allow_media_download=True,local_asr=local,downloader=lambda url,p,**k:p.write_bytes(b'audio'))
    result = provider.extract(request())
    assert result.error_code == 'tingwu_auth_required'
    assert result.metadata['asr_backend'] == 'asr.tingwu'


@pytest.mark.parametrize('payload', [[], None, {'ok':True,'segments':[]}, {'ok':True,'segments':[{'text':'words','start_seconds':0}],'coverage_end_seconds':100}])
def test_backend_rejects_bad_envelopes_and_incomplete_coverage(tmp_path, payload):
    from openmd.runtime.tingwu import TingwuASRBackend
    audio = tmp_path/'audio.mp3'
    audio.write_bytes(b'fixture')
    result = TingwuASRBackend(allow_cloud=True,duration_reader=lambda p:700,runner=lambda *a,**k:(0,json.dumps(payload),'')).transcribe(audio)
    assert result.status == 'failed'
    assert result.error_code == 'tingwu_protocol_invalid'


def test_bridge_parser_checks_identity_paragraph_count_and_coverage():
    from pathlib import Path
    import subprocess
    from openmd.runtime.managed_tools import node_dir
    node = node_dir()
    if node is None:
        pytest.skip('Node is optional for offline Python consumers')
    module = (Path(__file__).resolve().parents[1] / 'src/openmd/runtime/tingwu_bridge.mjs').as_uri()
    script = f'''import {{parseTranscript}} from {json.dumps(module)};
    import assert from 'node:assert/strict';
    const payload = {{tag:{{showName:'unique'}}, result:JSON.stringify({{pg:[{{sc:[{{bt:0,et:700000,tc:'original'}}]}}]}})}};
    assert.equal(parseTranscript(payload, 'unique', 700).segments[0].text, 'original');
    assert.throws(() => parseTranscript(payload, 'different', 700), /identity/);
    assert.equal(parseTranscript(payload, 'unique', 800), null);
    payload.result = JSON.stringify({{pg:[{{sc:[{{et:700000,tc:'no start'}}]}}]}});
    assert.equal(parseTranscript(payload, 'unique', 700), null);
    '''
    done = subprocess.run([str(node / ('node.exe' if __import__('os').name == 'nt' else 'node')), '--input-type=module', '-e', script], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode(errors='replace')
