"""Adversarial CLI receipt regressions, using only local fixtures."""
import contextlib
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import openmd.cli as cli
from openmd.backends.base import DiscoveryResult
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.package_writer import write_item_package
from openmd.core.package_validator import PackageValidationError
from openmd.core.runlog import append_runlog_entry, build_runlog_entry


def invoke(tmp_path, arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main([*arguments, '--out', str(tmp_path), '--receipt', 'compact'])
    assert len(stdout.getvalue().splitlines()) == 1, stderr.getvalue()
    payload = json.loads(stdout.getvalue())
    summary = json.loads((tmp_path / payload['summary_ref']).read_text()) if payload['summary_ref'] else None
    return code, payload, summary


def fake_row(url, args, status='verified'):
    append_runlog_entry(cli._runlog_path(args), build_runlog_entry(
        input_url=url, platform='web_article', backend='mock', status=status,
        source_type='article', source_level='native_text'))


def test_empty_batch_is_zero_discovered_blocked(tmp_path):
    inputs = tmp_path / 'empty.txt'
    inputs.write_text('')
    code, payload, summary = invoke(tmp_path, ['batch', str(inputs), '--backend', 'mock'])
    assert code == 1 and payload['status'] == 'blocked'
    assert payload['counts']['discovered'] == 0
    assert summary['coverage'] is None
    assert 'empty_scope' in summary['limitations']


def test_long_xiaoe_ingest_keeps_one_receipt_identity(tmp_path):
    url = 'https://demo.h5.xet.pomoho.com/content_page/' + 'a' * 250
    code, payload, _ = invoke(tmp_path, ['ingest', url, '--backend', 'mock'])
    assert code == 0
    assert payload['counts']['discovered'] == 1
    assert payload['counts']['verified'] == 1
    assert payload['counts']['unattempted'] == 0
    rows = [json.loads(line) for line in (tmp_path / 'runlog.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[0]['input_url'] == url


def test_long_urls_with_same_prefix_remain_distinct_receipt_items(tmp_path):
    prefix = 'https://example.com/' + 'a' * 250
    urls = [prefix + '/first?token=secret', prefix + '/second?token=secret']
    inputs = tmp_path / 'urls.txt'
    inputs.write_text('\n'.join(urls), encoding='utf-8')
    code, payload, _ = invoke(tmp_path, ['batch', str(inputs), '--backend', 'mock'])
    assert code == 0
    assert payload['counts']['discovered'] == 2
    assert payload['counts']['verified'] == 2
    assert payload['counts']['unattempted'] == 0
    rows = [json.loads(line) for line in (tmp_path / 'runlog.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len({row['input_url'] for row in rows}) == 2
    assert all('secret' not in row['input_url'] for row in rows)


def test_guardrail_accounts_for_remaining_batch_inputs(tmp_path, monkeypatch):
    inputs = tmp_path / 'inputs.txt'
    inputs.write_text('\n'.join(f'https://example.com/{i}' for i in range(3)))
    def failed(url, args, *, command):
        fake_row(url, args, 'failed')
        return cli.CliResult(ok=False, command=command, failed_count=1)
    monkeypatch.setattr(cli, '_process_input', failed)
    _, payload, summary = invoke(tmp_path, ['batch', str(inputs), '--guardrail-failure-streak', '1'])
    assert payload['counts']['discovered'] == 3
    assert payload['counts']['unattempted'] == 2
    assert not summary['execution_complete']


def test_prior_item_is_durable_before_next_input_and_interrupt_releases_lock(tmp_path, monkeypatch):
    inputs = tmp_path / 'inputs.txt'
    inputs.write_text('https://example.com/1\nhttps://example.com/2')
    calls = []
    def process(url, args, *, command):
        if calls:
            run = args._compact_receipt_run
            assert len(run.items_path.read_text().splitlines()) == 1
            assert json.loads(run.checkpoint_path.read_text())['completed'] == 1
            raise KeyboardInterrupt()
        calls.append(url)
        fake_row(url, args)
        return cli.CliResult(ok=True, command=command, success_count=1)
    monkeypatch.setattr(cli, '_process_input', process)
    code, payload, _ = invoke(tmp_path, ['batch', str(inputs), '--package'])
    assert code == 1
    assert payload['counts']['verified'] == 1
    assert payload['counts']['unattempted'] == 1
    assert not (tmp_path / '.openmd-compact.lock').exists()


def test_processing_io_failure_prints_receipt_and_releases_lock(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('simulated full disk')
    monkeypatch.setattr(cli, '_process_input', fail)
    code, payload, _ = invoke(tmp_path, ['ingest', 'https://example.com/1', '--package'])
    assert code == 1 and payload['status'] == 'failed'
    assert not (tmp_path / '.openmd-compact.lock').exists()


@pytest.mark.parametrize('duplicates', [False, True])
def test_incomplete_manifest_discovery_is_not_claimed_complete(tmp_path, monkeypatch, duplicates):
    item = ContentItem(platform='douyin', input_type='collection', url='https://www.douyin.com/video/1',
                       id='1', title='fixture', author=None, source_type='video', raw_meta={})
    class Executor:
        def discover(self, request):
            return DiscoveryResult.success(provider='fixture', items=[item, item] if duplicates else [item],
                                           metadata={'source_exhausted': False})
    monkeypatch.setattr(cli, '_build_discovery_executor', lambda *a, **k: Executor())
    code, payload, summary = invoke(tmp_path, ['collect', 'https://www.douyin.com/user/fixture',
                                             '--all', '--enable-browser-backends', '--manifest-only'])
    assert code == 1
    assert not summary['discovery_complete']
    assert payload['counts']['discovered'] == 1
    assert payload['counts']['input_occurrences'] == (2 if duplicates else 1)


def test_compact_contract_change_never_upgrades_existing_package(tmp_path):
    args = cli.build_parser().parse_args(['ingest', 'https://example.com/1', '--out', str(tmp_path),
                                         '--backend', 'mock', '--package', '--receipt', 'compact'])
    old = ContentItem(platform='web_article', input_type='single', url='https://example.com/1',
                      id='1', title='fixture', author=None, source_type='article',
                      raw_meta={'extraction_contract_version': 'v1'})
    new = replace(old, raw_meta={'extraction_contract_version': 'v2'})
    first = cli._compact_execution_fingerprint(old, args, ('mock',))
    written = write_item_package(old, TranscriptResult(text='body', status='verified', source_level='native',
                                 backend='mock', extraction_contract_version='v1'), tmp_path, execution_fingerprint=first)
    before = (written.package_path / 'metadata.json').read_bytes()
    with pytest.raises(PackageValidationError):
        cli._maybe_skip_existing_package(new, args, execution_fingerprint=cli._compact_execution_fingerprint(new, args, ('mock',)))
    assert (written.package_path / 'metadata.json').read_bytes() == before


def test_asr_effective_environment_changes_block_cached_package(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(['ingest', 'fixture.wav', '--out', str(tmp_path),
                                         '--package', '--receipt', 'compact'])
    item = ContentItem(platform='local_audio', input_type='single', url=str(tmp_path / 'fixture.wav'),
                       id='audio', title='fixture', author=None, source_type='audio', raw_meta={})
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_MODEL', 'small')
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_LANGUAGE', 'en')
    fingerprint = cli._compact_execution_fingerprint(item, args, ('asr.faster_whisper',))
    package = write_item_package(item, TranscriptResult(text='fixture', status='verified', source_level='native',
                                 backend='mock'), tmp_path, execution_fingerprint=fingerprint)
    before = {p.name: p.read_bytes() for p in package.package_path.iterdir() if p.is_file()}
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_MODEL', 'large-v3')
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_LANGUAGE', 'zh')
    changed = cli._compact_execution_fingerprint(item, args, ('asr.faster_whisper',))
    assert changed != fingerprint
    with pytest.raises(PackageValidationError, match='execution_fingerprint_mismatch'):
        cli._maybe_skip_existing_package(item, args, execution_fingerprint=changed)
    assert before == {p.name: p.read_bytes() for p in package.package_path.iterdir() if p.is_file()}


def test_collection_dedupes_before_extraction(tmp_path, monkeypatch):
    item = ContentItem(platform='youtube', input_type='collection', url='https://www.youtube.com/watch?v=1',
                       id='1', title='fixture', author=None, source_type='video', raw_meta={})
    class Executor:
        def discover(self, request):
            return DiscoveryResult.success(provider='fixture', items=[item, item])
    calls = []
    def process(items, args, *, command):
        calls.extend(items)
        for value in items:
            append_runlog_entry(cli._runlog_path(args), build_runlog_entry(
                input_url=value.url, platform=value.platform, backend='mock', status='verified',
                source_type='video', source_level='native_text'))
        return cli.CliResult(ok=True, command=command, success_count=len(items))
    monkeypatch.setattr(cli, '_build_discovery_executor', lambda *a, **k: Executor())
    monkeypatch.setattr(cli, '_process_items', process)
    code, payload, _ = invoke(tmp_path, ['collect', 'https://www.youtube.com/@fixture/videos'])
    assert code == 0 and len(calls) == 1
    assert payload['counts']['duplicate_inputs'] == 1


def test_writer_itself_blocks_contract_upgrade_even_if_fingerprint_matches(tmp_path):
    item = ContentItem(platform='web_article', input_type='single', url='https://example.com/1',
                       id='1', title='fixture', author=None, source_type='article', raw_meta={'extraction_contract_version': 'v1'})
    result = TranscriptResult(text='fixture', status='verified', source_level='native', backend='mock', extraction_contract_version='v1')
    package = write_item_package(item, result, tmp_path, execution_fingerprint='same')
    before = (package.package_path / 'metadata.json').read_bytes()
    with pytest.raises(PackageValidationError, match='extraction_contract_mismatch'):
        write_item_package(replace(item, raw_meta={'extraction_contract_version': 'v2'}),
                           replace(result, extraction_contract_version='v2'), tmp_path, execution_fingerprint='same')
    assert (package.package_path / 'metadata.json').read_bytes() == before


def test_collection_checkpoint_is_durable_before_second_backend_call(tmp_path, monkeypatch):
    items = [ContentItem(platform='youtube', input_type='collection', url=f'https://www.youtube.com/watch?v={i}',
                         id=str(i), title=f'fixture{i}', author=None, source_type='video', raw_meta={}) for i in range(2)]
    class Discovery:
        def discover(self, request):
            return DiscoveryResult.success(provider='fixture', items=items)
    class Backend:
        def resolve_item_identity(self, item, chain):
            return SimpleNamespace(ok=True, item=item)
        def extract(self, item, chain, *, allow_fallback):
            if item.id == '1':
                checkpoint = next((tmp_path / 'runs').glob('*/checkpoint.json'))
                assert json.loads(checkpoint.read_text())['completed'] == 1
                assert len((checkpoint.parent / 'items.jsonl').read_text().splitlines()) == 1
                raise KeyboardInterrupt()
            return TranscriptResult(text='fixture body', status='verified', source_level='native', backend='mock')
    monkeypatch.setattr(cli, '_build_discovery_executor', lambda *a, **k: Discovery())
    monkeypatch.setattr(cli, '_build_backend_executor', lambda *a, **k: Backend())
    code, payload, summary = invoke(tmp_path, ['collect', 'https://www.youtube.com/@fixture/videos', '--package'])
    assert code == 1 and payload['counts']['verified'] == 1
    assert payload['counts']['unattempted'] == 1
    assert not summary['execution_complete']
    assert not (tmp_path / '.openmd-compact.lock').exists()


@pytest.mark.parametrize('extension', ['.wav', '.mp4'])
def test_real_media_adapter_fingerprint_tracks_effective_asr(tmp_path, monkeypatch, extension):
    from openmd.adapters.local_audio.adapter import LocalAudioAdapter

    source = tmp_path / ('fixture' + extension)
    source.write_bytes(b'local fixture; no decoding performed')
    item = LocalAudioAdapter().resolve_items(str(source))[0]
    assert item.platform == 'generic_media'
    args = cli.build_parser().parse_args(['ingest', str(source), '--out', str(tmp_path),
                                         '--backend', 'asr.faster_whisper', '--package', '--receipt', 'compact'])
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_MODEL', 'small')
    first = cli._compact_execution_fingerprint(item, args, ('asr.faster_whisper',))
    monkeypatch.setenv('OPENMD_FASTER_WHISPER_MODEL', 'large-v3')
    assert cli._compact_execution_fingerprint(item, args, ('asr.faster_whisper',)) != first


def test_collection_aliases_reconcile_expected_and_occurrences(tmp_path, monkeypatch):
    items = [ContentItem(platform='youtube', input_type='collection', url=f'https://www.youtube.com/{i}',
                         id=str(i), title='fixture', author=None, source_type='video', raw_meta={}) for i in range(2)]
    class Discovery:
        def discover(self, request):
            return DiscoveryResult.success(provider='fixture', items=items)
    calls = []
    class Backend:
        def resolve_item_identity(self, item, chain):
            return SimpleNamespace(ok=True, item=replace(item, url='https://www.youtube.com/watch?v=canonical'))
        def extract(self, item, chain, *, allow_fallback):
            calls.append(item)
            return TranscriptResult(text='body', status='verified', source_level='native', backend='mock')
    monkeypatch.setattr(cli, '_build_discovery_executor', lambda *a, **k: Discovery())
    monkeypatch.setattr(cli, '_build_backend_executor', lambda *a, **k: Backend())
    code, payload, summary = invoke(tmp_path, ['collect', 'https://www.youtube.com/@fixture/videos'])
    assert code == 0 and len(calls) == 1
    assert payload['counts']['input_occurrences'] == 2
    assert payload['counts']['duplicate_inputs'] == 1
    assert summary['accounting_complete']
    run_dir = (tmp_path / payload['summary_ref']).parent
    assert set(json.loads((run_dir / 'expected.json').read_text())) == {
        json.loads(line)['stable_identity'] for line in (run_dir / 'items.jsonl').read_text().splitlines()}


def test_batch_empty_adapter_result_cannot_disappear(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.adapter import LocalAudioAdapter
    sources = [tmp_path / f'{i}.wav' for i in range(2)]
    for source in sources:
        source.write_bytes(b'fixture')
    inputs = tmp_path / 'inputs.txt'
    inputs.write_text('\n'.join(str(p) for p in sources))
    original = LocalAudioAdapter.resolve_items
    def resolve(self, value, **kwargs):
        return [] if value == str(sources[1]) else original(self, value, **kwargs)
    monkeypatch.setattr(LocalAudioAdapter, 'resolve_items', resolve)
    code, payload, summary = invoke(tmp_path, ['batch', str(inputs), '--backend', 'local_audio.mock'])
    assert code == 1 and payload['counts']['discovered'] == 2
    assert payload['counts']['blocked'] == 1
    assert summary['accounting_complete']
