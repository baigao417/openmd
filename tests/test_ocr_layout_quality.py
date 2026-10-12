"""OCR confidence cannot certify multi-column reading order."""
import importlib.machinery
import sys
import types
from unittest.mock import patch
from openmd.adapters.screenshot.backends.rapidocr import RapidOCRBackend


def test_side_by_side_rows_are_partial_even_with_high_confidence(tmp_path):
    source=tmp_path/'columns.png'
    source.write_bytes(b'fixture supplied by injected OCR')
    module=types.ModuleType('rapidocr_onnxruntime')
    module.__spec__=importlib.machinery.ModuleSpec(module.__name__,loader=None)
    module.RapidOCR=lambda:lambda source:([
        ([[0,0],[100,0],[100,20],[0,20]],'left first',.999),
        ([[300,0],[400,0],[400,20],[300,20]],'right first',.999),
        ([[0,40],[100,40],[100,60],[0,60]],'left second',.999),
        ([[300,40],[400,40],[400,60],[300,60]],'right second',.999),
    ],.01)
    with patch.dict(sys.modules,{module.__name__:module}):
        result=RapidOCRBackend().recognize(str(source),title='fixture')
    assert result.status=='partial'
    assert result.metadata['reading_order']=='ambiguous'
    assert len(result.metadata['source_map'])==4
    assert any('reading order' in note.lower() for note in result.source_notes)

def test_ocr_locators_survive_package_and_drop_untrusted_fields(tmp_path):
    from openmd.adapters.screenshot.adapter import ScreenshotOcrAdapter
    from openmd.core.models import TranscriptResult
    from openmd.core.package_writer import write_item_package
    from openmd.core.package_validator import validate_package
    import json
    source=tmp_path/'image.png';source.write_bytes(b'fixture')
    item=ScreenshotOcrAdapter().resolve_items(str(source))[0]
    result=TranscriptResult(text='left\nright',status='partial',source_level='ocr',backend='ocr.rapidocr',metadata={'source_map':[{'line':1,'bbox':[0,0,100,20],'url':'https://example.com/?token=private'}]})
    package=write_item_package(item,result,tmp_path/'out').package_path
    sources=(package/'sources.json').read_text()
    assert 'private' not in sources
    entry=json.loads(sources)['sources'][0]['source_map'][0]
    assert entry['source_ref']['bbox']==[0,0,100,20]
    assert entry['confidence'] is None
    assert validate_package(package).ok
