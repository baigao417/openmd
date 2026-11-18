from dataclasses import replace

from openmd.core.models import ContentItem, TranscriptResult


def media():
    return ContentItem('local_video', 'single', 'https://example.test/video', id='1', source_type='video', raw_meta={'duration': 300})


def transcript(text='[00:00 --> 04:59] original speech', **kwargs):
    return TranscriptResult(text, kwargs.pop('status', 'verified'), 'asr', 'test', **kwargs)


def test_truncated_transcript_downgrades_with_numeric_gap():
    from openmd.core.completeness import check_completeness
    result = check_completeness(media(), transcript('[00:00 --> 01:00] original speech'))
    assert result.status == 'partial'
    check = result.metadata['completeness']['checks'][0]
    assert check['status'] == 'downgrade'
    assert check['gap'] == {'start_seconds': 60.0, 'end_seconds': 300.0}
    assert result.source_level == 'asr'


def test_complete_missing_signals_and_partial_are_monotonic():
    from openmd.core.completeness import check_completeness
    assert check_completeness(media(), transcript()).status == 'verified'
    no_signal = replace(media(), raw_meta={})
    unknown = check_completeness(no_signal, transcript())
    assert unknown.metadata['completeness']['status'] == 'unknown'
    assert unknown.status == 'verified'
    assert check_completeness(media(), transcript(status='partial')).status == 'partial'
    failed = transcript(status='failed')
    assert check_completeness(media(), failed).status == 'failed'


def test_timestamp_start_is_not_cue_end():
    from openmd.core.completeness import check_completeness
    result = check_completeness(media(), transcript('[00:01] one long chunk'))
    assert result.metadata['completeness']['status'] == 'unknown'
    assert result.status == 'verified'


def test_native_whisper_cue_format_and_threshold_boundary():
    from openmd.core.completeness import check_completeness
    truncated = check_completeness(media(), transcript('[00:00:00-00:01:00] words'))
    assert truncated.status == 'partial'
    assert check_completeness(media(), transcript('[00:00:00-00:04:00] words')).status == 'verified'


def test_collection_counts_independent_check_without_changing_provenance():
    from openmd.core.completeness import check_completeness
    from openmd.core.coverage import CollectionCoverage
    coverage = CollectionCoverage()
    coverage.record_result(check_completeness(media(), transcript('[00:00 --> 01:00] words')))
    assert coverage.as_dict()['partial'] == 1
    assert coverage.as_dict()['completeness_counts'] == {'downgrade': 1}


def test_cue_text_end_wins_over_stale_summary_after_truncation():
    from openmd.core.completeness import check_completeness
    result = transcript('[00:00:00-00:01:00] only retained cue', metadata={'coverage_end_seconds': 300})
    assert check_completeness(media(), result).status == 'partial'


def test_optional_pdf_probe_import_failure_is_unknown(tmp_path, monkeypatch):
    import builtins
    from openmd.core.completeness import check_completeness
    original_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'openmd.ocr.documents':
            raise ImportError('optional parser unavailable')
        return original_import(name, *args, **kwargs)
    path = tmp_path / 'source.pdf'
    path.write_bytes(b'%PDF-1.4\n')
    item = ContentItem('pdf', 'single', path.as_uri(), source_type='document')
    monkeypatch.setattr(builtins, '__import__', missing)
    result = check_completeness(item, transcript('body'))
    assert result.status == 'verified'
    assert result.metadata['completeness']['status'] == 'unknown'


def test_pdf_missing_pages_and_duplicate_page_ids():
    from openmd.core.completeness import check_completeness
    item = ContentItem('pdf', 'single', 'https://example.test/doc.pdf', source_type='document')
    result = transcript('page one', metadata={'pages': [{'page': 1, 'status': 'verified'}, {'page': 1, 'status': 'verified'}]})
    checked = check_completeness(item, result, signals={'pdf_page_count': 3})
    assert checked.status == 'partial'
    assert checked.metadata['completeness']['checks'][0]['gap'] == {'missing_pages': [2, 3]}


def test_frontmatter_and_runlog_retain_completeness(tmp_path):
    from openmd.core.completeness import check_completeness
    from openmd.core.output import write_item_result
    from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry
    checked = check_completeness(media(), transcript('[00:00 --> 01:00] speech'))
    path = write_item_result(media(), checked, tmp_path)
    assert 'completeness:' in path.read_text(encoding='utf-8')
    assert '60' in path.read_text(encoding='utf-8')
    entry = append_runlog_entry(tmp_path / 'log.jsonl', build_item_runlog_entry(media(), checked))
    assert entry['completeness']['status'] == 'downgrade'
