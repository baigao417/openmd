"""Document migration boundaries that must hold outside the developer machine."""
import builtins
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

from openmd.engines.base import EngineLimits
from openmd.engines.native_common import Document


def test_document_extra_has_no_classifier_or_onnx_dependency():
    import re
    source = (Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text(encoding='utf-8')
    extra = re.search(r'^document\s*=\s*\[(.*?)\]', source, re.M).group(1)
    assert all(name not in extra for name in ('markitdown', 'magika', 'onnxruntime', 'rapidocr'))
    assert all(name in extra for name in ('mammoth', 'python-pptx', 'openpyxl', 'xlrd', 'olefile', 'pdfminer.six'))


def test_declared_chains_match_runtime():
    from openmd.engines.registry import engine_chain
    from openmd.routing.generate import declarations
    for row in declarations()['document_formats']:
        for suffix in row['extensions']:
            assert row['engines'] == [engine.id for engine in engine_chain(suffix)]


def test_missing_ocr_dependency_preserves_original_image(tmp_path, monkeypatch):
    original_import = builtins.__import__
    def load(name, *args, **kwargs):
        if name == 'PIL':
            raise ImportError('optional OCR dependency absent')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', load)
    document = Document(tmp_path / 'source.docx', EngineLimits(allow_ocr=True))
    document.add('Original source body')
    document.image(b'original bytes', 'word/media/image.png')
    result = document.result()
    assert result.status == 'partial'
    assert 'data:image/png;base64,' in result.text
    assert result.components[-1].error_code == 'embedded_image_ocr_unavailable'


def test_bundled_pandoc_detected_without_download(tmp_path, monkeypatch):
    from openmd.engines.tools import executable
    module = tmp_path / 'pypandoc/__init__.py'
    binary = module.parent / 'files' / ('pandoc.exe' if os.name == 'nt' else 'pandoc')
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'local executable fixture')
    monkeypatch.delenv('OPENMD_PANDOC', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr('openmd.engines.tools.load_local_config', lambda: {})
    monkeypatch.setattr('openmd.engines.tools.shutil.which', lambda name: None)
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: SimpleNamespace(origin=str(module)) if name == 'pypandoc' else None)
    assert executable('pandoc') == str(binary)
