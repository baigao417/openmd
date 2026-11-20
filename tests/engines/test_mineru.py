from pathlib import Path

from openmd.engines.tools import MinerUEngine
from openmd.engines.base import EngineError


def test_mineru_missing_returns_action_without_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv('OPENMD_MINERU', raising=False)
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: None)
    engine = MinerUEngine()
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.7 fake')
    monkeypatch.setattr('openmd.engines.tools.validate_document', lambda *_: b'%PDF-1.7')
    result = engine.convert(source)
    assert result.error_code == 'engine_unavailable'
    assert 'Install MinerU yourself' in result.metadata.get('next_action', '')


def test_mineru_detects_executable_and_reports_version(monkeypatch, tmp_path):
    fake = tmp_path / 'mineru-fake'
    fake.write_text('# fake MinerU CLI marker\n', encoding='utf-8')
    calls = []
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: str(fake))

    def version(argv, *_):
        calls.append(argv)
        return 'MinerU 4.2.3\n'

    monkeypatch.setattr('openmd.engines.tools.run_command', version)
    report = MinerUEngine().available()

    assert report == {'ok': True, 'reason': 'User-installed MinerU executable detected.', 'version': 'MinerU 4.2.3'}
    assert calls == [[str(fake), '--version']]


def test_mineru_builds_basic_command_and_collects_markdown(monkeypatch, tmp_path):
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.7 fake')
    calls = []
    monkeypatch.setattr('openmd.engines.tools.validate_document', lambda *_: b'%PDF-1.7')
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: 'mineru-fake')
    monkeypatch.setattr(MinerUEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': '4.2.3'})
    monkeypatch.setattr('openmd.engines.tools.owned_temporary_directory', lambda **_: str(tmp_path / 'work'))
    (tmp_path / 'work').mkdir()
    def run(argv, directory, limits):
        calls.append(argv)
        target = Path(argv[argv.index('--output') + 1])
        (target / 'scan').mkdir(parents=True)
        (target / 'scan' / 'scan.md').write_text('# Page 1\n\n你好', encoding='utf-8')
        return ''
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    class Lock:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr('openmd.runtime.gpu_lock.gpu_lock', lambda **kw: Lock())
    result = MinerUEngine().convert(source)
    assert result.error_code is None, (result.error_code, result.error_summary)
    assert result.status == 'verified'
    assert result.source_level == 'ocr'
    assert result.text == '# Page 1\n\n你好'
    assert calls[0][1:3] == ['parse', str(source.resolve())]
    assert calls[0][calls[0].index('--tier') + 1] == 'basic'


def test_mineru_failure_is_structured_and_standard_tier_is_selected(monkeypatch, tmp_path):
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.7 fake')
    calls = []
    monkeypatch.setattr('openmd.engines.tools.validate_document', lambda *_: b'%PDF-1.7')
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: 'mineru-fake')
    monkeypatch.setattr(MinerUEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': '4.2.3'})
    monkeypatch.setattr('openmd.engines.tools.owned_temporary_directory', lambda **_: str(tmp_path / 'work'))
    (tmp_path / 'work').mkdir()
    def fail(argv, *_):
        calls.append(argv)
        raise EngineError('document_conversion_failed')
    monkeypatch.setattr('openmd.engines.tools.run_command', fail)
    class Lock:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr('openmd.runtime.gpu_lock.gpu_lock', lambda **kw: Lock())
    result = MinerUEngine().convert(source, tier='standard')
    assert result.error_code == 'mineru_failed'
    assert calls[0][calls[0].index('--tier') + 1] == 'standard'


def test_mineru_gpu_wait_timeout_is_structured(monkeypatch, tmp_path):
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.7 fake')
    monkeypatch.setattr('openmd.engines.tools.validate_document', lambda *_: b'%PDF-1.7')
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: 'mineru-fake')
    monkeypatch.setattr(MinerUEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': '4.2.3'})
    class Lock:
        def __init__(self, **_): pass
        def __enter__(self): raise TimeoutError('lock busy')
        def __exit__(self, *_): pass
    monkeypatch.setattr('openmd.runtime.gpu_lock.gpu_lock', lambda **kw: Lock())
    result = MinerUEngine().convert(source)
    assert result.error_code == 'mineru_timeout'


def test_mineru_process_timeout_is_structured(monkeypatch, tmp_path):
    source = tmp_path / 'scan.pdf'
    source.write_bytes(b'%PDF-1.7 fake')
    monkeypatch.setattr('openmd.engines.tools.validate_document', lambda *_: b'%PDF-1.7')
    monkeypatch.setattr('openmd.engines.tools.executable', lambda _tool: 'mineru-fake')
    monkeypatch.setattr(MinerUEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': '4.2.3'})

    def timeout(*_):
        raise EngineError('document_timeout')

    monkeypatch.setattr('openmd.engines.tools.run_command', timeout)

    class Lock:
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr('openmd.runtime.gpu_lock.gpu_lock', lambda **_: Lock())
    result = MinerUEngine().convert(source)
    assert result.error_code == 'mineru_timeout'
