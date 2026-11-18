import pytest
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_ipynb(tmp_path, case):
    path = tmp_path / 'source.ipynb'
    data = '{"nbformat": 4, "cells": [{"cell_type": "markdown", "source": ["Original \\u4e2d\\u6587"]}, {"cell_type": "code", "source": ["print(7)"], "outputs": [{"output_type": "stream", "text": ["7"]}]}]}'
    if case == 'broken':
        data = '\x00\ufffd'
    elif case == 'encrypted':
        data = '-----BEGIN PGP MESSAGE-----\nENCRYPTED'
    elif case == 'empty':
        data = ''
    path.write_text(data, encoding='utf-8')
    result = NativeEngine('ipynb').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'print(7)' in result.text
        assert result.backend == 'openmd.ipynb'
    else:
        assert result.status == 'failed'
