import struct
import pytest
from native_fixtures import ole_container
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


def word_document(text, encrypted=False):
    payload = text.encode('utf-16le')
    word = bytearray(512 + len(payload))
    struct.pack_into('<HH', word, 0, 0xa5ec, 0xc1)
    struct.pack_into('<H', word, 10, 0x100 if encrypted else 0)
    struct.pack_into('<II', word, 24, 512, 512+len(payload))
    struct.pack_into('<H', word, 32, 14)
    struct.pack_into('<H', word, 62, 22)
    struct.pack_into('<I', word, 76, len(text))
    struct.pack_into('<H', word, 152, 34)
    plc = struct.pack('<IIHIH', 0, len(text), 0, 512, 0)
    clx = b'\x02' + struct.pack('<I', len(plc)) + plc
    struct.pack_into('<II', word, 418, 0, len(clx))
    word[512:] = payload
    return ole_container({'WordDocument':bytes(word),'0Table':clx})


@pytest.mark.parametrize('case', ['positive','broken','empty','encrypted','oversize'])
def test_doc_native(tmp_path, case):
    if case == 'positive':
        pytest.importorskip('olefile')
    path = tmp_path / 'source.doc'
    data = word_document('' if case == 'empty' else 'Original 中文\rName\x07Value\x07\rApple\x077\x07', encrypted=case=='encrypted')
    path.write_bytes(b'bad' if case == 'broken' else data)
    result = NativeEngine('doc').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original 中文' in result.text and '| Apple | 7 |' in result.text
        assert result.metadata['text_character_coverage'] == 1
    else:
        assert result.status == 'failed'
