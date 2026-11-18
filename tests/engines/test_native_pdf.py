import pytest
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


def pdf_bytes(text='Original source words', encrypted=False):
    stream = ('BT /F1 12 Tf 50 100 Td (' + text + ') Tj ET').encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>', b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    if encrypted:
        objects.append(b'<< /Filter /Standard /V 1 /R 2 /O <0000000000000000000000000000000000000000000000000000000000000000> /U <0000000000000000000000000000000000000000000000000000000000000000> /P -4 >>')
    data = bytearray(b'%PDF-1.4\n')
    offsets = []
    for index,obj in enumerate(objects,1):
        offsets.append(len(data))
        data.extend(str(index).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    offset = len(data)
    data.extend(b'xref\n0 '+str(len(objects)+1).encode()+b'\n0000000000 65535 f \n')
    data.extend(b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets))
    data.extend(b'trailer\n<< /Size '+str(len(objects)+1).encode()+b' /Root 1 0 R'+(b' /Encrypt 6 0 R /ID [<1234><1234>]' if encrypted else b'')+b' >>\nstartxref\n'+str(offset).encode()+b'\n%%EOF')
    return data


@pytest.mark.parametrize('case',['positive','broken','empty','encrypted','oversize'])
def test_pdf_native(tmp_path,case):
    pytest.importorskip('pdfminer')
    path = tmp_path / 'source.pdf'
    path.write_bytes(b'bad' if case=='broken' else pdf_bytes('' if case=='empty' else 'Original source words', encrypted=case=='encrypted'))
    result = NativeEngine('pdf').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case=='positive':
        assert result.has_usable_text and 'Original source words' in result.text
    else:
        assert result.status=='failed'

