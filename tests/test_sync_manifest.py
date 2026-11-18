import json
from concurrent.futures import ThreadPoolExecutor


def test_sync_update_unchanged_conflict_and_restored(tmp_path):
    from openmd.core.sync_manifest import SyncManifest
    manifest = SyncManifest(tmp_path)
    first = manifest.write('fake:1', 'fake/title.md', 'one')
    assert first.operation == 'created'
    assert manifest.write('fake:1', 'fake/renamed.md', 'one').operation == 'unchanged'
    changed = manifest.write('fake:1', 'fake/renamed.md', 'two')
    assert changed.path == first.path
    assert changed.operation == 'updated'
    first.path.write_text('user edit', encoding='utf-8')
    conflict = manifest.write('fake:1', 'fake/title.md', 'three')
    assert conflict.operation == 'conflict_kept_user_edit'
    assert conflict.path.name == 'title.openmd-update.md'
    assert first.path.read_text(encoding='utf-8') == 'user edit'
    first.path.unlink()
    restored = manifest.write('fake:1', 'fake/renamed.md', 'four')
    assert restored.path == first.path
    assert restored.operation == 'restored_missing'


def test_corrupt_manifest_backed_up_and_existing_file_preserved(tmp_path):
    from openmd.core.sync_manifest import SyncManifest
    root = tmp_path / '.openmd'
    root.mkdir()
    (root / 'manifest.json').write_text('{broken', encoding='utf-8')
    (tmp_path / 'title.md').write_text('user text', encoding='utf-8')
    written = SyncManifest(tmp_path).write('fake:1', 'title.md', 'new')
    assert written.path != tmp_path / 'title.md'
    assert list(root.glob('manifest.corrupt-*.json'))
    assert (tmp_path / 'title.md').read_text(encoding='utf-8') == 'user text'


def test_concurrent_manifest_writes_keep_all_records(tmp_path):
    from openmd.core.sync_manifest import SyncManifest
    def write(i):
        return SyncManifest(tmp_path).write(f'fake:{i}', 'title.md', str(i))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(18)))
    data = json.loads((tmp_path / '.openmd/manifest.json').read_text(encoding='utf-8'))
    assert len(data['entries']) == 18
    assert len({r.path for r in results}) == 18


def test_same_source_parallel_writes_and_traversal(tmp_path):
    import pytest
    from openmd.core.sync_manifest import SyncManifest
    def write(i):
        return SyncManifest(tmp_path).write('fake:one', 'title.md', 'same')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert sum(r.operation == 'created' for r in results) == 1
    assert sum(r.operation == 'unchanged' for r in results) == 7
    with pytest.raises(ValueError):
        SyncManifest(tmp_path).write('fake:bad', '../outside.md', 'bad')


def test_default_writer_unchanged_and_declared_syncable_updates(tmp_path):
    from openmd.core.models import ContentItem, TranscriptResult
    from openmd.core.output import write_item_result
    result = TranscriptResult('original', 'verified', 'native_text', 'test')
    item = ContentItem('ai_chat', 'single', 'https://example.test/chat/1', id='1', title='Chat')
    path = write_item_result(item, result, tmp_path)
    assert write_item_result(item, result, tmp_path) == path
    default = ContentItem('web_article', 'single', 'https://example.test/article', title='Article')
    assert write_item_result(default, result, tmp_path) != write_item_result(default, result, tmp_path)


def test_sync_writer_reports_operations_and_never_overwrites_edited_update(tmp_path):
    from openmd.core.models import ContentItem, TranscriptResult
    from openmd.core.output import write_item_result
    item = ContentItem('ai_chat', 'single', 'https://example.test/chat/1', id='1', title='Chat')
    result = TranscriptResult('original', 'verified', 'native_text', 'test')
    receipt = {}
    first = write_item_result(item, result, tmp_path, sync_receipt=receipt)
    assert receipt == {'operation': 'created'}
    write_item_result(item, result, tmp_path, sync_receipt=receipt)
    assert receipt == {'operation': 'unchanged'}
    first.write_text('user edit', encoding='utf-8')
    update = write_item_result(item, result, tmp_path, sync_receipt=receipt)
    update.write_text('edited update', encoding='utf-8')
    next_update = write_item_result(item, result, tmp_path, sync_receipt=receipt)
    assert next_update != update
    assert update.read_text(encoding='utf-8') == 'edited update'
    assert receipt == {'operation': 'conflict_kept_user_edit'}


def test_sync_manifest_append_keeps_one_document_and_preserves_previous_content(tmp_path):
    from openmd.core.sync_manifest import SyncManifest
    manifest = SyncManifest(tmp_path)
    first = manifest.write(
        'wechat_chat:fixture', 'wechat_chat/title.md',
        '---\ntitle: T\n---\n\n# T\n\n### A\n\nold\n\n## Source\n\nfooter\n',
    )
    second = manifest.write(
        'wechat_chat:fixture', 'wechat_chat/title.md',
        '---\ntitle: T\n---\n\n# T\n\n### B\n\nnew\n\n## Source\n\nfooter\n',
        append=True, cursor='m2',
    )
    text = second.path.read_text(encoding='utf-8')
    assert second.operation == 'updated'
    assert second.path == first.path
    assert 'old' in text and 'new' in text
    assert text.count('## Source') == 1


def test_syncable_package_explicitly_rejected_before_stale_skip(tmp_path):
    import argparse
    import pytest
    from openmd.cli_items import _maybe_skip_existing_package
    from openmd.core.models import ContentItem
    from openmd.core.package_validator import PackageValidationError
    with pytest.raises(PackageValidationError, match='sync_requires_markdown_output'):
        _maybe_skip_existing_package(ContentItem('ai_chat', 'single', 'https://example.test/chat/1'), argparse.Namespace(package=True))


def test_cross_process_writes(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    code = 'from openmd.core.sync_manifest import SyncManifest; import sys; SyncManifest(sys.argv[1]).write(sys.argv[2], "title.md", sys.argv[2])'
    processes = [subprocess.Popen([sys.executable, '-c', code, str(tmp_path), f'fake:{i}'], env=env) for i in range(5)]
    assert [p.wait(timeout=20) for p in processes] == [0] * 5
    data = json.loads((tmp_path / '.openmd/manifest.json').read_text(encoding='utf-8'))
    assert len(data['entries']) == 5
