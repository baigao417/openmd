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
    assert checked.metadata['completeness']['checks'][0]['gap'] == {
        'missing_pages': [2, 3], 'output_pages': 1, 'total_pages': 3,
    }


def test_unverified_textlayer_pages_are_output_not_missing():
    from openmd.core.completeness import check_completeness
    from openmd.core.renderer import _completeness_frontmatter
    item = ContentItem('pdf', 'single', 'https://example.test/doc.pdf', source_type='document')
    pages = [{'page': 1, 'status': 'verified'}, {'page': 2, 'status': 'partial', 'textlayer_unverified': True},
             {'page': 3, 'status': 'partial', 'textlayer_unverified': True}]
    checked = check_completeness(item, transcript('pages', metadata={'pages': pages}), signals={'pdf_page_count': 3})
    assert checked.status == 'partial'
    gap = checked.metadata['completeness']['checks'][0]['gap']
    assert gap == {'missing_pages': [], 'output_pages': 3, 'unverified_pages': 2, 'total_pages': 3}
    assert '已输出 3/3 页，其中 2 页文字层未核验' in _completeness_frontmatter(checked)


def test_missing_and_unverified_pages_are_reported_together():
    from openmd.core.completeness import check_completeness
    from openmd.core.renderer import _completeness_frontmatter
    item = ContentItem('pdf', 'single', 'https://example.test/doc.pdf', source_type='document')
    pages = [{'page': 1, 'status': 'partial', 'textlayer_unverified': True}, {'page': 2, 'status': 'partial', 'reason': 'ocr_permission_required'}]
    checked = check_completeness(item, transcript('pages', metadata={'pages': pages}), signals={'pdf_page_count': 2})
    assert checked.metadata['completeness']['checks'][0]['gap']['missing_pages'] == [2]
    assert '输出 1/2 页，其中 1 页文字层未核验' in _completeness_frontmatter(checked)


def test_frontmatter_and_runlog_retain_completeness(tmp_path):
    from openmd.core.completeness import check_completeness
    from openmd.core.output import write_item_result
    from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry
    checked = check_completeness(media(), transcript('[00:00 --> 01:00] speech'))
    path = write_item_result(media(), checked, tmp_path)
    assert 'completeness:' in path.read_text(encoding='utf-8')
    assert '00:01:00' in path.read_text(encoding='utf-8')
    entry = append_runlog_entry(tmp_path / 'log.jsonl', build_item_runlog_entry(media(), checked))
    assert entry['completeness']['status'] == 'downgrade'


def test_frontmatter_omits_ok_completeness(tmp_path):
    from openmd.core.output import write_item_result
    checked = transcript('[00:00:00-00:04:00] complete enough')
    from openmd.core.completeness import check_completeness
    path = write_item_result(media(), check_completeness(media(), checked), tmp_path)
    text = path.read_text(encoding='utf-8')
    assert 'completeness:' not in text


def test_frontmatter_omits_unknown_completeness(tmp_path):
    from openmd.core.output import write_item_result
    from openmd.core.completeness import check_completeness
    unknown_item = replace(media(), raw_meta={})
    checked = check_completeness(unknown_item, transcript('[00:01] start only'))
    path = write_item_result(unknown_item, checked, tmp_path)
    assert 'completeness:' not in path.read_text(encoding='utf-8')


def test_frontmatter_downgrade_is_human_readable_scalar(tmp_path):
    from openmd.core.output import write_item_result
    from openmd.core.completeness import check_completeness
    path = write_item_result(media(), check_completeness(media(), transcript('[00:00:00-00:00:02] truncated')), tmp_path)
    text = path.read_text(encoding='utf-8')
    line = next(line for line in text.splitlines() if line.startswith('completeness:'))
    assert '{' not in line and '\\"' not in line
    assert 'partial' in line and '00:00:02' in line and '00:05:00' in line


def test_pdf_frontmatter_downgrade_reports_output_pages(tmp_path):
    from openmd.core.output import write_item_result
    from openmd.core.completeness import check_completeness
    item = ContentItem('pdf', 'single', 'https://example.test/doc.pdf', source_type='document')
    result = transcript('page one', metadata={'pages': [{'page': 1, 'status': 'verified'}]})
    checked = check_completeness(item, result, signals={'pdf_page_count': 3})
    path = write_item_result(item, checked, tmp_path)
    line = next(line for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('completeness:'))
    assert 'partial' in line and '输出 1/3 页' in line
    assert '{' not in line and '\\"' not in line


def test_conversation_stats_count_downgrades_short_export():
    from openmd.core.completeness import check_completeness
    item = ContentItem('wechat_chat', 'collection', 'wechat:fixture', source_type='conversation')
    result = TranscriptResult(
        text='body', status='verified', source_level='manual_export', backend='wx-cli.export',
        metadata={'exported_count': 2, 'stats_count': 3, 'limit': 500},
    )
    checked = check_completeness(item, result)
    assert checked.status == 'partial'
    assert checked.metadata['completeness']['checks'][0]['reason'] == 'export_count_below_stats'


def test_conversation_limit_hit_downgrades_verified_export():
    from openmd.core.completeness import check_completeness
    item = ContentItem('wechat_chat', 'collection', 'wechat:fixture', source_type='conversation')
    result = TranscriptResult(
        text='body', status='verified', source_level='manual_export', backend='wx-cli.export',
        metadata={'exported_count': 3, 'stats_count': 3, 'limit': 3, 'limit_hit': True},
    )
    assert check_completeness(item, result).status == 'partial'


def test_conversation_checks_combine_turn_and_count_evidence():
    # A conversation carries both a page-turn check and an export/stats count
    # check; usually only one has evidence. One proven check with the other
    # unknown is still independently complete; any proven gap still downgrades.
    from openmd.core.completeness import check_completeness
    from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
    item = ContentItem('ai_chat', 'single', 'https://grok.com/c/fixture', source_type='conversation')
    result = TranscriptResult('body', 'verified', 'browser_rendered', 'fixture',
                              blocks=[TurnBlock('user', 'q'), TurnBlock('assistant', 'a')])
    ok = check_completeness(item, result, signals={'conversation_turn_count': 2})
    assert ok.status == 'verified'
    assert ok.metadata['completeness']['status'] == 'ok'
    short = replace(result, metadata={'exported_count': 2, 'stats_count': 5})
    gap = check_completeness(item, short, signals={'conversation_turn_count': 2})
    assert gap.status == 'partial'
    assert gap.metadata['completeness']['status'] == 'downgrade'
    neither = check_completeness(item, result, signals={})
    assert neither.metadata['completeness']['status'] == 'unknown'
