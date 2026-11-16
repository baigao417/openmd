"""Core media transport contract, independent of installed tools and accounts."""
from types import SimpleNamespace

import pytest

from openmd.runtime import media


def test_media_download_supports_proxy_transport_without_local_protocols(monkeypatch, tmp_path):
    target = tmp_path / 'audio.mp3'
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
