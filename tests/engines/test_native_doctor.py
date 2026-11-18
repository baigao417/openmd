from openmd.engines.diagnostics import document_formats


def test_doctor_formats_explain_missing_dependencies(monkeypatch):
    monkeypatch.setattr('openmd.engines.tools.executable', lambda name: None)
    monkeypatch.setattr('openmd.engines.native.NativeEngine.available', lambda self: {'ok': self.format not in {'docx','msg'}, 'reason': 'missing test parser', 'version': None})
    monkeypatch.setattr('openmd.engines.markitdown.MarkItDownEngine.available', lambda self: {'ok':False,'reason':'missing baseline','version':None})
    report = document_formats()
    assert {'docx','pptx','xlsx','doc','ppt','xls','pdf','zip','mobi','azw3'} <= report.keys()
    assert report['mobi']['available'] is False
    assert 'calibre' in report['mobi']['install_command'].lower()
    assert report['csv']['available'] is True
    assert report['docx']['available'] is False
    assert 'pip install' in report['docx']['install_command']
    assert report['docx']['engines'][0]['id'] == 'openmd.docx'
