import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.run_gate import validate_gate
from openmd.routing.generate import check, declarations
from openmd.runtime.interpreters import owned_popen, terminate_quietly
from openmd.runtime.startup_receipt import wait_for_session


def test_generated_declarations_match_runtime_projections():
    root = Path(__file__).resolve().parents[1]
    assert check(root) == []
    from openmd.core.operation_contract import COLLECTION_BINDINGS
    from openmd.routing.loader import load_catalog, default_backend_chains
    catalog = load_catalog()
    assert catalog
    for source, binding in declarations()['collection_bindings'].items():
        assert COLLECTION_BINDINGS[source] == (binding[0], tuple(binding[1]))
    assert default_backend_chains(effective=False)['xiaoe'][0] == 'opencli.xiaoe.transcript'


def test_generated_drift_is_rejected(tmp_path):
    from openmd.routing.generate import projections
    for path, text in projections(tmp_path).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    target = tmp_path/'src/openmd/routing/cards/builtin.json'
    target.write_text('{}', encoding='utf-8')
    assert 'src/openmd/routing/cards/builtin.json' in check(tmp_path)


@pytest.mark.parametrize('pair', json.loads((Path(__file__).parent/'fixtures/contracts/content-pairs.json').read_text(encoding='utf-8')), ids=lambda pair: pair['name'])
def test_state_page_and_legal_neighbor(pair):
    item = ContentItem(platform=pair['platform'],input_type='single',url='https://example.invalid/article',source_type='article')
    def result(text):
        return TranscriptResult(text=text,status='verified',source_level='native_text',backend='fixture')
    assert validate_extraction_content(result(pair['negative']),item).status == 'failed'
    good = validate_extraction_content(result(pair['positive']),item)
    assert good.status == 'verified'
    assert good.text == pair['positive']


def gates(tmp_path):
    authority = {'schema':'openmd.run-gate/v1','actor':'user','goal':'test','scope':[str(tmp_path)],'allowed_side_effects':['test','write','commit'],
                 'authority_source':'user-task','evidence_ref':'approved brief','unknown':False}
    gate = {**authority,'actor':'worker','allowed_side_effects':['test'], 'authority_sha256':hashlib.sha256(json.dumps(authority,sort_keys=True).encode()).hexdigest()}
    return gate,authority


@pytest.mark.parametrize('actor',['user','dispatcher','worker'])
def test_authority_identity_and_actor_do_not_grant_new_permissions(tmp_path, actor):
    gate,authority = gates(tmp_path)
    gate['actor'] = actor
    validate_gate(gate,authority,action='test',target=tmp_path)
    with pytest.raises(ValueError,match='action_not_authorized'):
        validate_gate(gate,authority,action='cloud',target=tmp_path)
    gate['allowed_side_effects'].append('cloud')
    with pytest.raises(ValueError,match='authority_escalation'):
        validate_gate(gate,authority,action='cloud',target=tmp_path)


def test_worker_cannot_supersede_user_or_escape_scope(tmp_path):
    gate,authority = gates(tmp_path)
    gate['supersedes'] = 'user-decision'
    with pytest.raises(ValueError,match='cannot_supersede'):
        validate_gate(gate,authority,action='test',target=tmp_path)
    gate.pop('supersedes')
    with pytest.raises(ValueError,match='out_of_scope'):
        validate_gate(gate,authority,action='test',target=tmp_path.parent)
    authority['actor'] = 'dispatcher'
    with pytest.raises(ValueError,match='user_authority_missing'):
        validate_gate(gate,authority,action='test',target=tmp_path)


@pytest.mark.parametrize('mode',['session','exit','silent'])
def test_startup_receipt_is_bounded_and_requires_session_identity(mode):
    session = '12345678-1234-1234-1234-123456789abc'
    code = {'session':f'import json; print(json.dumps({{"type":"thread.started","thread_id":"{session}"}}),flush=True)',
            'exit':'print("done",flush=True)', 'silent':'import time; time.sleep(10)'}[mode]
    process = owned_popen([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,text=True)
    start = time.monotonic()
    try:
        if mode == 'session':
            assert wait_for_session(process,timeout=2)['session_id'] == session
        else:
            with pytest.raises((ValueError,TimeoutError)):
                wait_for_session(process,timeout=.15)
        assert time.monotonic()-start < 3
    finally:
        terminate_quietly(process)


def test_deterministic_tempdir_identity_unknown_is_not_dead(monkeypatch):
    from openmd.runtime import tempdirs
    # Existing identity checks must remain conservative; no process needs to be
    # killed and no filesystem scheduler timing is part of this assertion.
    monkeypatch.setattr(tempdirs, 'pid_start_time', lambda pid: None)
    assert tempdirs.pid_alive(__import__('os').getpid()) is not False
