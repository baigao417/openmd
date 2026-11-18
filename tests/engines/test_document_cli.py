"""Default CLI route and persisted package receipt checks without optional tools."""
import json
from zipfile import ZipFile

from openmd.cli import main


def test_default_cli_reports_missing_optional_engine(tmp_path, monkeypatch, capsys):
    from openmd.engines.tools import CalibreEngine
    monkeypatch.setattr(CalibreEngine, 'available', lambda self: {'ok': False, 'reason': 'Install Calibre ebook-convert.', 'version': None})
    source = tmp_path / 'book.fb2'
    source.write_text('<FictionBook><body><section><p>Original source text.</p></section></body></FictionBook>')
    out = tmp_path / 'out'
    assert main(['ingest', str(source), '--out', str(out), '--package']) == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload['ok']
    rows = [json.loads(line) for line in (out / 'runlog.jsonl').read_text().splitlines()]
    assert rows[-1]['error_code'] == 'engine_unavailable'
    assert rows[-1]['backend'] == 'document.engines'


def test_zip_media_gate_is_saved_as_per_entry_receipt(tmp_path, capsys):
    source = tmp_path / 'media.zip'
    with ZipFile(source, 'w') as archive:
        archive.writestr('sound.mp3', b'ID3')
        archive.writestr('video.mp4', b'ftyp')
    out = tmp_path / 'out'
    assert main(['ingest', str(source), '--out', str(out), '--package']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok']
    package = next(out.rglob('*.openmd'))
    metadata = json.loads((package / 'metadata.json').read_text())
    assert metadata['status'] == 'partial'
    assert len(metadata['components']) == 2
    assert all(c['error_code'] == 'asr_required' for c in metadata['components'])
    assert 'asr_required' in (package / 'index.md').read_text()
    assert any('openmd.zip=selected' in w for w in metadata['warnings'])


def test_table_truncation_coverage_survives_runlog_writer(tmp_path, monkeypatch, capsys):
    from openmd.engines.markitdown import MarkItDownEngine
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': 'test'})
    monkeypatch.setattr(MarkItDownEngine, '_convert_text', lambda *a: '| name | value |\n|---|---|\n| Original | 1 |')
    source = tmp_path / 'large.csv'
    source.write_text('name,value\n' + 'Original,1\n' * 10005)
    out = tmp_path / 'out'
    assert main(['ingest', str(source), '--out', str(out), '--package']) == 0
    capsys.readouterr()
    log = json.loads((out / 'runlog.jsonl').read_text().splitlines()[-1])
    assert log['status'] == 'partial'
    coverage = log['source_scope']['coverage']
    assert coverage['expected_count'] == 10005
    assert coverage['fetched_count'] == 10000
    assert coverage['missing_count'] == 5
    assert coverage['complete'] is False
    assert log['source_scope']['engine'] == 'markitdown'
