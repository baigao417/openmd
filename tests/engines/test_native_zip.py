from zipfile import ZipFile, ZIP_DEFLATED
import pytest

from openmd.engines.archive import ZipEngine
from openmd.engines.base import EngineLimits


def test_zip_uses_native_text_formats_and_preserves_media_gate(tmp_path):
    path = tmp_path / 'source.zip'
    with ZipFile(path, 'w') as z:
        z.writestr('data.csv', 'Name,Value\nOriginal,7')
        z.writestr('body.fb2', '<FictionBook><body><p>Original chapter</p></body></FictionBook>')
        z.writestr('sound.mp3', b'ID3')
    result = ZipEngine().convert(path)
    assert result.status == 'partial'
    assert 'Original chapter' in result.text and '| Original | 7 |' in result.text
    rows = result.metadata['archive_entries']
    assert [r['backend'] for r in rows[:2]] == ['openmd.csv', 'openmd.fb2']
    assert rows[2]['error_code'] == 'asr_required'


@pytest.mark.parametrize('case', ['broken','empty','encrypted','oversize'])
def test_zip_boundaries(tmp_path, case):
    path = tmp_path / 'source.zip'
    with ZipFile(path, 'w') as z:
        if case != 'empty':
            z.writestr('body.csv','Name,Value\nOriginal,7')
    data = bytearray(path.read_bytes())
    if case == 'broken':
        data = b'bad'
    if case == 'encrypted':
        local = data.index(b'PK\x03\x04')
        central = data.index(b'PK\x01\x02')
        data[local+6] |= 1
        data[central+8] |= 1
    path.write_bytes(data)
    limits = EngineLimits(max_archive_bytes=1,max_file_bytes=1) if case=='oversize' else None
    assert ZipEngine().convert(path, limits).status == 'failed'



def test_zip_extracts_when_the_temp_root_is_not_canonical(tmp_path, monkeypatch):
    # macOS hands out /var/... temp dirs that resolve to /private/var/...; any
    # non-canonical temp root must still extract (it used to fail as invalid container).
    from contextlib import contextmanager
    import openmd.engines.archive as archive

    (tmp_path / 'real').mkdir()
    (tmp_path / 'detour').mkdir()

    @contextmanager
    def non_canonical_root(prefix=''):
        yield str(tmp_path / 'detour' / '..' / 'real')

    monkeypatch.setattr(archive, 'owned_temporary_directory', non_canonical_root)
    path = tmp_path / 'source.zip'
    with ZipFile(path, 'w') as z:
        z.writestr('data.csv', 'Name,Value\nOriginal,7')
    result = ZipEngine().convert(path)
    assert result.status == 'verified', result.error_code
    assert '| Original | 7 |' in result.text
