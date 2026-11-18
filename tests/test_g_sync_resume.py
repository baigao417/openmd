from dataclasses import replace

from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
from openmd.core.output import write_item_result


def _item():
    return ContentItem('ai_chat', 'single', 'https://example.test/chat/fixture', id='fixture', title='Fixture',
                       source_type='conversation', raw_meta={'sync_cursor_enabled': True})


def _result(text, cursor, append=False):
    return TranscriptResult(text, 'verified', 'manual_export', 'fixture', blocks=[TurnBlock('A', text)],
                            metadata={'source_id': 'fixture:stable', 'cursor': cursor, 'sync_append': append})


def test_resume_reads_stable_alias_and_missing_artifact_requires_full_export(tmp_path):
    from openmd.core.sync_manifest import prepare_sync_item
    item = _item()
    cursor = {'since': '2026-01-01', 'message_ids': ['m1']}
    path = write_item_result(item, _result('old fixture', cursor), tmp_path)
    resumed = prepare_sync_item(item, tmp_path)
    assert resumed.raw_meta['sync_cursor'] == cursor
    assert resumed.raw_meta['sync_source_id'] == 'fixture:stable'
    assert 'sync_cursor' not in item.raw_meta
    path.unlink()
    assert 'sync_cursor' not in prepare_sync_item(item, tmp_path).raw_meta


def test_append_real_renderer_keeps_one_header_footer_and_rerun_unchanged(tmp_path):
    item = _item()
    path = write_item_result(item, _result('old fixture', {'since': '2026-01-01'}), tmp_path)
    write_item_result(item, _result('new fixture', {'since': '2026-01-02'}, True), tmp_path)
    text = path.read_text(encoding='utf-8')
    assert text.count('# Fixture\n') == 1
    assert text.count('来源说明：') == 1
    assert text.count('old fixture') == text.count('new fixture') == 1
    before = path.read_bytes()
    result = replace(_result('', {'since': '2026-01-02'}, True), blocks=[], metadata={
        'source_id': 'fixture:stable', 'sync_no_delta': True, 'sync_append': True})
    receipt = {}
    assert write_item_result(item, result, tmp_path, sync_receipt=receipt) == path
    assert receipt['operation'] == 'unchanged'
    assert path.read_bytes() == before


def test_append_conflict_retains_cursor_and_all_previous_turns(tmp_path):
    from openmd.core.sync_manifest import prepare_sync_item
    item = _item()
    path = write_item_result(item, _result('old fixture', 'm1'), tmp_path)
    path.write_text(path.read_text(encoding='utf-8') + '\nuser edit\n', encoding='utf-8')
    receipt = {}
    update = write_item_result(item, _result('new fixture', 'm2', True), tmp_path, sync_receipt=receipt)
    assert receipt['operation'] == 'conflict_kept_user_edit'
    assert 'user edit' in path.read_text(encoding='utf-8')
    assert 'old fixture' in update.read_text(encoding='utf-8')
    assert 'new fixture' in update.read_text(encoding='utf-8')
    assert prepare_sync_item(item, tmp_path).raw_meta['sync_cursor'] == 'm1'


def test_empty_content_cannot_claim_unchanged_without_persisted_source():
    from openmd.backends.executor import BackendExecutor
    class Provider:
        def extract(self, request):
            return TranscriptResult('', 'verified', 'manual_export', 'fixture', metadata={'sync_no_delta': True})
    result = BackendExecutor({'fixture': Provider()}).extract(_item(), ('fixture',))
    assert result.status == 'failed'
