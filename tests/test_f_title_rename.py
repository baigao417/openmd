"""Synthetic title changes must preserve identity, edits and other sources."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from openmd.core.sync_manifest import SyncManifest


@pytest.mark.parametrize('body', ['one', 'two'])
def test_unedited_canonical_renames_atomically_and_updates_manifest(tmp_path, monkeypatch, body):
    import openmd.core.sync_manifest as module
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1', 'old.md', 'one')
    renames = []
    original = module.os.replace
    def record(source, destination):
        if source == first.path:
            renames.append((source, destination))
        return original(source, destination)
    monkeypatch.setattr(module.os, 'replace', record)
    result = manifest.write('fake:1', 'new.md', body)
    assert result.path == tmp_path / 'new.md'
    assert result.renamed_from == 'old.md'
    assert renames == [(first.path, result.path)]
    assert not first.path.exists()
    assert result.path.read_text(encoding='utf-8') == body
    entry = manifest.read('fake:1')
    assert entry['path'] == 'new.md'
    assert entry['content_sha256'] == hashlib.sha256(result.path.read_bytes()).hexdigest()
    again = manifest.write('fake:1', 'new.md', body)
    assert again.path == result.path and again.operation == 'unchanged'
    assert again.renamed_from is None


def test_rename_does_not_touch_user_edit_or_move_manifest(tmp_path):
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1', 'old.md', 'one')
    entry = manifest.read('fake:1')
    first.path.write_text('Synthetic user edit.', encoding='utf-8')
    result = manifest.write('fake:1', 'new.md', 'two')
    assert result.operation == 'conflict_kept_user_edit'
    assert getattr(result, 'renamed_from', None) is None
    assert manifest.read('fake:1') == entry
    assert first.path.read_text(encoding='utf-8') == 'Synthetic user edit.'
    assert not (tmp_path / 'new.md').exists()


@pytest.mark.parametrize('occupied_body', ['one', 'other'])
def test_rename_collision_uses_suffix_and_repeats_without_suffix_drift(tmp_path, occupied_body):
    manifest = SyncManifest(tmp_path)
    other = manifest.write('fake:other', 'new.md', occupied_body)
    first = manifest.write('fake:1', 'old.md', 'one')
    result = manifest.write('fake:1', 'new.md', 'one')
    assert result.path == tmp_path / 'new-2.md'
    assert result.renamed_from == 'old.md'
    assert not first.path.exists()
    again = manifest.write('fake:1', 'new.md', 'one')
    assert again.path == result.path and again.renamed_from is None
    assert other.path.read_text(encoding='utf-8') == occupied_body
    assert manifest.read('fake:other')['path'] == 'new.md'


def test_rename_reserves_other_sources_missing_canonical_name(tmp_path):
    manifest = SyncManifest(tmp_path)
    other = manifest.write('fake:other', 'new.md', 'other')
    other.path.unlink()
    manifest.write('fake:1', 'old.md', 'one')
    result = manifest.write('fake:1', 'new.md', 'two')
    assert result.path == tmp_path / 'new-2.md'
    restored = manifest.write('fake:other', 'new.md', 'other')
    assert restored.operation == 'restored_missing'
    assert restored.path == other.path
    assert result.path.read_text(encoding='utf-8') == 'two'


def test_missing_canonical_restores_original_path_without_claiming_rename(tmp_path):
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1', 'old.md', 'one')
    first.path.unlink()
    result = manifest.write('fake:1', 'new.md', 'two')
    assert result.path == first.path and result.operation == 'restored_missing'
    assert getattr(result, 'renamed_from', None) is None


@pytest.mark.parametrize('failure', ['rename', 'content', 'manifest'])
def test_rename_write_failure_keeps_original_and_manifest_retryable(tmp_path, monkeypatch, failure):
    import openmd.core.sync_manifest as module
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1', 'old.md', 'one')
    before = manifest.path.read_bytes()
    original_write, original_replace = module._atomic_write, module.os.replace
    def fail_write(path, content):
        if (failure == 'content' and path == tmp_path / 'new.md') or (failure == 'manifest' and path == manifest.path):
            raise OSError('synthetic failure')
        return original_write(path, content)
    def fail_replace(source, destination):
        if failure == 'rename' and source == first.path:
            raise OSError('synthetic failure')
        return original_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(module, '_atomic_write', fail_write)
        patch.setattr(module.os, 'replace', fail_replace)
        with pytest.raises(OSError, match='synthetic failure'):
            manifest.write('fake:1', 'new.md', 'two')
    assert first.path.read_text(encoding='utf-8') == 'one'
    assert manifest.path.read_bytes() == before
    assert not (tmp_path / 'new.md').exists()
    retried = manifest.write('fake:1', 'new.md', 'two')
    assert retried.path == tmp_path / 'new.md'
    assert retried.renamed_from == 'old.md'


def test_no_delta_title_change_renames_only_matching_canonical_hash(tmp_path):
    manifest = SyncManifest(tmp_path)
    manifest.write('fake:1', 'old.md', 'one')
    result = manifest.write('fake:1', 'new.md', '', no_delta=True)
    assert result.path == tmp_path / 'new.md' and result.renamed_from == 'old.md'
    assert result.path.read_text(encoding='utf-8') == 'one'
    result.path.write_text('Synthetic user edit.', encoding='utf-8')
    repeated = manifest.write('fake:1', 'other.md', '', no_delta=True)
    assert repeated.path == result.path and repeated.renamed_from is None
    assert not (tmp_path / 'other.md').exists()


def test_output_and_daily_save_include_renamed_from(tmp_path):
    from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
    from openmd.core.output import write_item_result
    from openmd.interactive import _save
    item = ContentItem('ai_chat', 'single', 'https://grok.com/c/synthetic', id='synthetic',
                       title='Old title', source_type='conversation')
    result = TranscriptResult('Synthetic question.', 'verified', 'browser_rendered', 'test',
                              blocks=[TurnBlock('user', 'Synthetic question.')])
    receipt = {}
    first = write_item_result(item,result,tmp_path/'direct',sync_receipt=receipt)
    new = write_item_result(replace(item,title='New title'),result,tmp_path/'direct',sync_receipt=receipt)
    assert new.name == 'New title.md' and not first.exists()
    assert receipt['renamed_from'] == 'ai_chat/Old title.md'
    # Reusing a receipt must not leak a previous rename to an unchanged save.
    write_item_result(replace(item,title='New title'),result,tmp_path/'direct',sync_receipt=receipt)
    assert 'renamed_from' not in receipt
    stage = tmp_path/'stage'
    staged = write_item_result(item,result,stage)
    first = _save(stage,tmp_path/'daily',title='Old title',source=staged)
    new = _save(stage,tmp_path/'daily',title='New title',source=staged,sync_receipt=receipt)
    assert new.name == 'New title.md' and not first.exists()
    assert receipt['renamed_from'] == 'Old title.md'


def test_snapshot_go_json_exposes_rename_at_final_destination(tmp_path, capsys):
    from openmd.cli import main
    page = tmp_path/'synthetic.html'
    def save(title):
        page.write_text(f'<title>{title} - ChatGPT</title><div data-message-author-role="user">Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div>',encoding='utf-8')
        assert main(['go',str(page),'--as-snapshot','--url','https://chatgpt.com/c/synthetic','-o',str(tmp_path/'saved'),'--json']) == 0
        return json.loads(capsys.readouterr().out)
    first = save('Old title')
    second = save('New title')
    assert second['saved_to'] != first['saved_to']
    assert second['sync']['renamed_from'] == 'Old title.md'
    assert not Path(first['saved_to']).exists()


def test_extraction_output_retains_rename_for_cli_receipt(tmp_path):
    from argparse import Namespace
    from openmd.cli_items import _write_extraction_output
    from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
    item = ContentItem('ai_chat','single','https://grok.com/c/synthetic',id='synthetic',title='Old title',source_type='conversation')
    result = TranscriptResult('Synthetic question.','verified','browser_rendered','test',blocks=[TurnBlock('user','Synthetic question.')])
    args = Namespace(out=str(tmp_path), package=False, overwrite=False, raw=False, download_assets=False)
    _write_extraction_output(item,result,args)
    renamed = _write_extraction_output(replace(item,title='New title'),result,args)
    assert renamed.renamed_from == 'ai_chat/Old title.md'


def test_rename_append_keeps_prior_content_and_advances_cursor(tmp_path):
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1','old.md','Synthetic previous turn.',cursor='one')
    result = manifest.write('fake:1','new.md','Synthetic next turn.',append=True,cursor='two')
    assert result.renamed_from == 'old.md' and not first.path.exists()
    assert result.path.read_text(encoding='utf-8') == 'Synthetic previous turn.\n\nSynthetic next turn.'
    assert manifest.read('fake:1')['cursor'] == 'two'


def test_rename_with_same_content_identity_preserves_original_export_clock(tmp_path):
    manifest = SyncManifest(tmp_path)
    manifest.write('fake:1','old.md','Synthetic original clock.',content_identity='stable')
    result = manifest.write('fake:1','new.md','Synthetic new clock.',content_identity='stable')
    assert result.operation == 'unchanged' and result.renamed_from == 'old.md'
    assert result.path.read_text(encoding='utf-8') == 'Synthetic original clock.'
