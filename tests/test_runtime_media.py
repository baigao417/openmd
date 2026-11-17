"""Core media transport contract, independent of installed tools and accounts."""
from types import SimpleNamespace

import pytest

from openmd.runtime import media


def test_prefetch_rewrites_only_core_owned_locators_and_keeps_key_in_memory(tmp_path, monkeypatch):
    import io
    playlist = '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:10\n#EXT-X-KEY:METHOD=AES-128,URI="key?sign=private",IV=0x0000000000000000000000000000000a\n#EXTINF:2,\na.ts?sign=private\n#EXTINF:2,\nb.ts?sign=private\n#EXT-X-ENDLIST\n'
    data = {'https://example.invalid/p.m3u8': playlist.encode(), 'https://example.invalid/key?sign=private': b'x'*16,
            'https://example.invalid/a.ts?sign=private': b'a'*16, 'https://example.invalid/b.ts?sign=private': b'b'*16}
    class Response(io.BytesIO):
        def geturl(self): return 'https://example.invalid/p.m3u8'
    monkeypatch.setattr(media, 'urlopen', lambda request, **kw: Response(data[request.full_url]))
    payload = media.prefetch_hls('https://example.invalid/p.m3u8', tmp_path, referer='https://example.invalid/', timeout=10)
    assert 'private' not in payload and 'https://' not in payload
    assert 'data:application/octet-stream;base64,' in payload
    # FFmpeg's file protocol expects a native absolute path after file:, not
    # percent-encoded file:///C:/... URLs (which break Windows crypto inputs).
    assert 'file:' + (tmp_path/'000000.ts').resolve().as_posix() in payload
    assert (tmp_path/'000000.ts').read_bytes() == b'a'*16
    assert (tmp_path/'000001.ts').read_bytes() == b'b'*16
    assert all(p.suffix == '.ts' for p in tmp_path.iterdir())


@pytest.mark.parametrize('extra', ['#EXT-X-BYTERANGE:50', '#EXT-X-MAP:URI="init"', '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"', '#EXT-X-DISCONTINUITY', '#EXT-X-STREAM-INF:BANDWIDTH=100'])
def test_prefetch_unsupported_playlists_fall_back_before_segment_download(extra, tmp_path, monkeypatch):
    import io
    calls = []
    class Response(io.BytesIO):
        def geturl(self): return 'https://example.invalid/p.m3u8'
    def fetch(request, **kw):
        calls.append(request.full_url)
        return Response(f'#EXTM3U\n{extra}\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n'.encode())
    monkeypatch.setattr(media, 'urlopen', fetch)
    assert media.prefetch_hls('https://example.invalid/p.m3u8', tmp_path, referer='https://example.invalid/', timeout=10) is None
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_prefetch_retries_only_failed_segment_and_preserves_order(tmp_path, monkeypatch):
    import io
    from collections import Counter
    calls = Counter()
    playlist = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n#EXT-X-ENDLIST\n'
    class Response(io.BytesIO):
        def geturl(self): return 'https://example.invalid/p.m3u8'
    def fetch(request, **kwargs):
        name = request.full_url.rsplit('/', 1)[1]
        calls[name] += 1
        if name == 'b.ts' and calls[name] == 1: raise OSError('simulated transport interruption')
        return Response(playlist if name == 'p.m3u8' else name.encode())
    monkeypatch.setattr(media, 'urlopen', fetch)
    output = media.prefetch_hls('https://example.invalid/p.m3u8', tmp_path, referer='https://example.invalid/', timeout=10)
    assert calls == {'p.m3u8':1, 'a.ts':1, 'b.ts':2}
    assert output.index('000000.ts') < output.index('000001.ts')
    assert (tmp_path/'000000.ts').read_bytes() == b'a.ts'
    assert (tmp_path/'000001.ts').read_bytes() == b'b.ts'


@pytest.mark.parametrize('locator', ['ftp://example.invalid/segment', 'file:///etc/passwd', 'https://user:placeholder@example.invalid/a'])
def test_prefetch_redirects_cannot_expand_protocol_or_credentials(locator):
    from urllib.request import Request
    with pytest.raises(ValueError, match='invalid_media_locator'):
        media._MediaRedirects().redirect_request(Request('https://example.invalid/a'),None,302,'Found',{},locator)


def test_prefetch_retries_truncated_content_length(tmp_path, monkeypatch):
    import io
    playlist = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n'
    requests = []
    class Response(io.BytesIO):
        def geturl(self): return 'https://example.invalid/p.m3u8'
        def getheader(self, key): return '4'
    def fetch(request, **kwargs):
        name = request.full_url.rsplit('/', 1)[1]
        requests.append(name)
        return Response(playlist if name == 'p.m3u8' else b'aa' if requests.count('a.ts') == 1 else b'aaaa')
    monkeypatch.setattr(media, 'urlopen', fetch)
    media.prefetch_hls('https://example.invalid/p.m3u8',tmp_path,referer='https://example.invalid/',timeout=10)
    assert requests.count('a.ts') == 2
    assert (tmp_path/'000000.ts').read_bytes() == b'aaaa'


def test_prefetch_rejects_truncated_aes_blocks_without_content_length(tmp_path, monkeypatch):
    import io
    calls = []
    playlist = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST\n'
    class Response(io.BytesIO):
        def geturl(self): return 'https://example.invalid/p.m3u8'
    def fetch(request, **kwargs):
        name = request.full_url.rsplit('/',1)[1]
        calls.append(name)
        return Response(playlist if name == 'p.m3u8' else b'k'*16 if name == 'key' else b'a'*15 if calls.count('a.ts') == 1 else b'a'*16)
    monkeypatch.setattr(media,'urlopen',fetch)
    media.prefetch_hls('https://example.invalid/p.m3u8',tmp_path,referer='https://example.invalid/',timeout=10)
    assert calls.count('a.ts') == 2
    assert (tmp_path/'000000.ts').stat().st_size == 16


def test_media_download_supports_proxy_transport_without_local_protocols(monkeypatch, tmp_path):
    target = tmp_path / 'audio.mp3'
    monkeypatch.setattr(media, 'prefetch_hls', lambda *a, **k: None)
    monkeypatch.setattr(media.shutil, 'which', lambda name: 'ffmpeg')

    def run(command, **kwargs):
        protocols = set(command[command.index('-protocol_whitelist') + 1].split(','))
        # HTTPS through the caller's existing HTTP proxy needs httpproxy.
        # Local file/concat/data protocols stay unavailable to remote playlists.
        assert protocols == {'http', 'https', 'tcp', 'tls', 'crypto', 'httpproxy'}
        assert command[command.index('-i') + 1] == 'https://media.example/audio.m3u8?sign=dummy'
        assert kwargs['capture_output'] and kwargs['timeout'] == 60
        target.write_bytes(b'fixture audio')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(media, 'run_interruptibly', run)
    media.download_audio('https://media.example/audio.m3u8?sign=dummy', target, referer='https://example.invalid/', timeout=60)
    assert target.is_file()


def test_media_failure_does_not_publish_signed_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(media, 'prefetch_hls', lambda *a, **k: None)
    monkeypatch.setattr(media.shutil, 'which', lambda name: 'ffmpeg')
    monkeypatch.setattr(media, 'run_interruptibly', lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr=b'https://media.example/?sign=private'))
    with pytest.raises(RuntimeError, match='^media_download_failed$') as error:
        media.download_audio('https://media.example/audio.m3u8?sign=private', tmp_path/'audio.mp3', referer='https://example.invalid/')
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('locator', ['file:///tmp/audio', 'https://user:pass@media.example/audio', 'ftp://media.example/audio'])
def test_media_non_http_or_credential_locator_is_rejected_before_launch(monkeypatch, tmp_path, locator):
    monkeypatch.setattr(media, 'run_interruptibly', lambda *args, **kwargs: pytest.fail('invalid locator launched'))
    with pytest.raises(ValueError, match='invalid_media_locator'):
        media.download_audio(locator, tmp_path/'audio.mp3', referer='https://example.invalid/')
