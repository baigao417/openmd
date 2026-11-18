from pathlib import Path
from unittest.mock import patch

import pytest

from openmd.core.capabilities import get_capability
from openmd.core.planner import build_plan


def test_youtube_data_api_all_has_explicit_network_binding():
    result = build_plan('https://www.youtube.com/playlist?list=PLfixture', request_all=True, available_gates={'network', 'browser', 'account'})
    assert result.ok
    assert result.source_exhausted_approved
    assert result.execution_ready
    assert result.discovery_provider == 'youtube.data-api.playlist.discovery'
    assert result.blocked_reason is None


@pytest.mark.parametrize('value', ['', 'not-a-valid-source', 'https://', 'ftp://example.com/file'])
def test_unknown_and_invalid_sources_are_not_web_ready(value):
    result = build_plan(value, available_gates={'network'})
    assert not result.ok
    assert not result.execution_ready


def test_missing_file_is_explicit(tmp_path):
    result = build_plan(str(tmp_path / 'missing.wav'), available_gates={'asr'})
    assert not result.execution_ready
    assert result.error_code == 'input_missing'


def test_office_route_uses_local_document_binding(tmp_path):
    path = tmp_path / 'fixture.docx'
    path.write_bytes(b'fixture')
    result = build_plan(str(path), available_gates={'local_compute'})
    assert result.detected_source == 'pdf_office'
    assert result.execution_ready
    assert result.runtime_provider == 'document.engines'
    assert result.blocked_reason is None


def test_relative_directory_never_enumerates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'fixture').mkdir()
    with patch.object(Path, 'rglob', side_effect=AssertionError('enumeration')), patch.object(Path, 'iterdir', side_effect=AssertionError('enumeration')):
        result = build_plan('fixture', available_gates={'network'})
    assert result.input_kind == 'folder'
    assert not result.execution_ready
    assert result.error_code == 'folder_source_not_inspected'


def test_real_local_file_is_stat_only_and_uses_real_backend(tmp_path):
    path = tmp_path / 'fixture.wav'
    path.write_bytes(b'fixture')
    original_open = Path.open

    def guard_input_open(open_path, *args, **kwargs):
        assert open_path.resolve() != path.resolve(), 'input content read'
        return original_open(open_path, *args, **kwargs)

    with patch.object(Path, 'open', guard_input_open):
        result = build_plan(str(path), available_gates={'asr'})
    assert result.execution_ready
    assert result.runtime_provider == 'asr.auto'
    assert result.required_backend == 'asr.auto'


def test_xhs_extraction_binding_is_not_discovery():
    result = build_plan('https://www.xiaohongshu.com/explore/fixture', available_gates={'browser'})
    assert result.execution_ready
    assert result.runtime_provider == 'xiaohongshu.composite'


def test_video_uses_implemented_local_backend(tmp_path):
    path = tmp_path / 'fixture.mp4'
    path.write_bytes(b'fixture')
    result = build_plan(str(path), available_gates={'asr'})
    assert result.runtime_provider == 'asr.auto'
    assert result.execution_ready


def test_remote_media_is_not_a_local_asr_input():
    result = build_plan('https://example.com/video.mp4', available_gates={'asr', 'network'})
    assert not result.execution_ready
    assert result.binding_state == 'unimplemented'


def test_weixin_collection_needs_discovery_browser_gate():
    result = build_plan('weixin://fixture', available_gates={'network'})
    assert result.input_kind == 'collection'
    assert not result.execution_ready
    assert 'browser' in result.required_gates


def test_youtube_bounded_collection_has_discovery_binding():
    result = build_plan('https://www.youtube.com/playlist?list=PLfixture', available_gates={'network', 'account'})
    assert result.execution_ready
    assert result.discovery_provider == 'youtube.data-api.playlist.discovery'


def test_existing_platform_bindings_are_visible():
    for source in ('bilibili', 'xiaoyuzhou', 'xiaoe', 'x', 'feishu', 'screenshot'):
        assert get_capability(source).binding_state == 'implemented'


def test_browser_youtube_route_is_available_without_network_gate():
    result = build_plan('https://www.youtube.com/playlist?list=PLfixture', available_gates={'browser'})
    assert result.execution_ready
    assert result.runtime_provider == 'opencli.youtube.transcript'
    assert result.required_backend == result.runtime_provider
    assert result.discovery_provider == 'opencli.youtube.playlist.discovery'
    assert result.required_discovery_backend == result.discovery_provider


def test_collection_permission_requires_input_collection_shape():
    result = build_plan('https://www.youtube.com/watch?v=abcdefghijk', input_kind='collection', available_gates={'network', 'account'})
    assert not result.execution_ready


@pytest.mark.parametrize('source, provider', [
    ('weixin', 'wechat-article-exporter.http'),
    ('douyin', 'opencli.openmd-douyin.creator-videos'),
    ('xiaohongshu', 'opencli.openmd-xiaohongshu.creator-notes'),
])
def test_shared_all_contract_requires_browser_and_matching_provider(source, provider):
    from openmd.core.operation_contract import collect_all_allowed
    assert collect_all_allowed(source, browser_enabled=True, discovery_provider=provider)
    assert not collect_all_allowed(source, browser_enabled=False, discovery_provider=provider)
    assert not collect_all_allowed(source, browser_enabled=True, discovery_provider='other')
    assert not collect_all_allowed('youtube', browser_enabled=True, discovery_provider=None)


def test_declared_extraction_bindings_are_registered_real_providers():
    from openmd.core.capabilities import APPROVED_CAPABILITIES
    from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
    options = BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True)
    for record in APPROVED_CAPABILITIES:
        if record.binding_state != 'implemented':
            continue
        platform = {'local_audio': 'generic_media', 'local_video': 'generic_media', 'tutorial_video': 'generic_media', 'pdf_office': 'pdf'}.get(record.source_id, record.source_id)
        registry = create_runtime_backend_provider_registry(platform, options)
        provider = registry.provider_for(record.runtime_provider)
        assert provider is not None, record.source_id
        assert type(provider).__name__ != 'SkeletonBackendProvider', record.source_id
        for attr in ('backend', 'parser'):
            assert not type(getattr(provider, attr, None)).__name__.startswith('Missing'), record.source_id


def test_all_weixin_and_douyin_have_current_operations():
    for value, gates in [('weixin://fixture', {'network', 'browser'}), ('https://www.douyin.com/user/fixture123', {'browser', 'media_download', 'asr'})]:
        result = build_plan(value, request_all=True, available_gates=gates)
        assert result.execution_ready
        assert result.discovery_provider is not None


def test_file_uri_and_relative_pdf_are_consistent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'fixture with spaces.pdf'
    path.write_bytes(b'fixture')
    for value in (path.as_uri(), str(path), path.name):
        result = build_plan(value, available_gates={'local_compute'})
        assert result.detected_source == 'pdf_office'
        assert result.execution_ready


def test_network_paths_never_stat():
    with patch.object(Path, 'stat', side_effect=AssertionError('network stat')):
        for value in ('file://server/share/a.wav', 'file:////server/share/a.wav', r'\\server\share\a.wav'):
            assert not build_plan(value, available_gates={'asr'}).execution_ready


def test_collection_cannot_be_relabelled_single():
    result = build_plan('https://www.youtube.com/playlist?list=PLfixture', input_kind='single', available_gates={'network'})
    assert not result.execution_ready
    assert result.error_code == 'input_shape_unsupported'


def test_standalone_douyin_resolution_stays_explicitly_blocked():
    result = build_plan('https://www.douyin.com/video/123456789', available_gates={'browser'})
    assert not result.execution_ready
    assert 'standalone Douyin' in result.blocked_reason


def test_youtube_homepage_is_not_a_video():
    result = build_plan('https://www.youtube.com/', available_gates={'network'})
    assert not result.execution_ready
