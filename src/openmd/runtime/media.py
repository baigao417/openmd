"""Core-owned transient media to audio conversion. No locators in failures."""
from pathlib import Path
import base64
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError

from .interpreters import run_interruptibly
from .tempdirs import owned_temporary_directory


def _http_locator(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('invalid_media_locator')


class _MediaRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _http_locator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, *, timeout):
    # urllib's default redirects also admit FTP. Match the old FFmpeg HTTP-only
    # boundary on every redirect and never install a cookie/auth handler.
    return build_opener(_MediaRedirects()).open(request, timeout=timeout)


def prefetch_hls(url: str, directory: Path, *, referer: str, timeout: float) -> str | None:
    """Four bounded fetches for simple finite MPEG-TS HLS; otherwise use FFmpeg.

    Signed media URLs and content decryption keys stay in memory. The generated
    playlist contains only core-owned TS paths and inline content keys. It is fed
    through stdin to a network-disabled FFmpeg invocation, never written to disk.
    """
    deadline = time.monotonic() + timeout
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': referer}
    def open_media(locator):
        _http_locator(locator)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('media_download_failed')
        return urlopen(Request(locator, headers=headers), timeout=min(30, remaining))
    with open_media(url) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        base = response.geturl()
    if len(raw) > 2 * 1024 * 1024:
        return None
    try:
        lines = raw.decode('utf-8-sig').splitlines()
    except UnicodeError:
        return None
    allowed = ('#EXTM3U', '#EXTINF:', '#EXT-X-VERSION:', '#EXT-X-TARGETDURATION:',
               '#EXT-X-MEDIA-SEQUENCE:', '#EXT-X-KEY:', '#EXT-X-ENDLIST',
               '#EXT-X-PLAYLIST-TYPE:', '#EXT-X-INDEPENDENT-SEGMENTS')
    if (not lines or lines[0].strip() != '#EXTM3U' or '#EXT-X-ENDLIST' not in lines
            or any(line.startswith('#EXT') and not line.startswith(allowed) for line in lines)):
        return None
    segments, keys, output = [], {}, []
    encrypted = False
    for line in lines:
        line = line.strip()
        if line.startswith('#EXT-X-KEY:'):
            # Narrow AES-128 identity keys only; preserve IV/media sequence.
            if line == '#EXT-X-KEY:METHOD=NONE':
                encrypted = False
                output.append(line)
                continue
            match = re.fullmatch(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"\r\n]+)"(?:,IV=(0x[0-9a-fA-F]{32}))?', line)
            if not match:
                return None
            key_url = urljoin(base, match[1])
            _http_locator(key_url)
            # Do not fetch keys until the complete playlist is validated.
            keys[key_url] = None
            encrypted = True
            output.append(('key', key_url, match[2]))
        elif line and not line.startswith('#'):
            locator = urljoin(base, line)
            _http_locator(locator)
            path = directory / f'{len(segments):06d}.ts'
            segments.append((locator, path, encrypted))
            # FFmpeg file: is a protocol prefix, not a browser file URI. In
            # particular file:///C:/... and percent escapes fail on Windows.
            output.append('file:' + path.resolve().as_posix())
        elif line.startswith('#EXT'):
            output.append(line)
    if not segments or len(segments) > 5000 or len(keys) > 100:
        return None
    for key_url in keys:
        with open_media(key_url) as response:
            key = response.read(17)
        if len(key) != 16:
            raise ValueError('media_download_failed')
        keys[key_url] = base64.b64encode(key).decode('ascii')
    total = 0
    lock = threading.Lock()
    cancelled = threading.Event()
    def fetch_segment(entry):
        nonlocal total
        if cancelled.is_set():
            raise TimeoutError('media_download_failed')
        locator, path, encrypted = entry
        for attempt in range(3):
            size = 0
            try:
                with open_media(locator) as response, path.open('wb') as stream:
                    declared = getattr(response, 'getheader', lambda key: None)('Content-Length')
                    expected_size = int(declared) if declared and declared.isdigit() else None
                    while True:
                        if cancelled.is_set() or time.monotonic() >= deadline:
                            raise TimeoutError('media_download_failed')
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        with lock:
                            total += len(chunk)
                            if size > 32 * 1024**2 or total > 2 * 1024**3:
                                raise ValueError('media_download_failed')
                        stream.write(chunk)
                if size == 0:
                    raise ValueError('media_download_failed')
                if expected_size is not None and size != expected_size:
                    # HTTPResponse.read(amt) can return EOF without raising
                    # IncompleteRead. Do not hand a truncated TS to FFmpeg.
                    raise OSError('media_download_failed')
                if encrypted and size % 16:
                    raise OSError('media_download_failed')
                return
            except OSError as exc:
                transient = not isinstance(exc, HTTPError) or exc.code in {408, 429, 500, 502, 503, 504}
                if transient and attempt < 2 and not cancelled.is_set() and time.monotonic() < deadline:
                    time.sleep(.2 * (attempt + 1))
                    continue
                cancelled.set()
                raise
            except BaseException:
                cancelled.set()
                raise
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='openmd-hls') as pool:
        list(pool.map(fetch_segment, segments))
    rendered = []
    for line in output:
        if isinstance(line, tuple):
            _, key_url, iv = line
            line = '#EXT-X-KEY:METHOD=AES-128,URI="data:application/octet-stream;base64,' + keys[key_url] + '"'
            if iv:
                line += ',IV=' + iv
        rendered.append(line)
    return '\n'.join(rendered) + '\n'


def download_audio(url: str, target: Path, *, referer: str, timeout: int = 600) -> dict:
    _http_locator(url)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg_missing")
    if urlsplit(url).path.lower().endswith('.m3u8'):
        started = time.monotonic()
        try:
            with owned_temporary_directory(prefix='openmd-hls-') as directory:
                playlist = prefetch_hls(url, Path(directory), referer=referer, timeout=timeout)
                if playlist is not None:
                    network_seconds = time.monotonic() - started
                    segments = list(Path(directory).glob('*.ts'))
                    network_bytes = sum(p.stat().st_size for p in segments)
                    # Only our generated local paths and in-memory content keys
                    # reach this allowlist. Original remote playlists never do.
                    command = [executable, '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
                        '-protocol_whitelist', 'file,pipe,data,crypto', '-allowed_extensions', 'ALL',
                        '-f', 'hls', '-i', 'pipe:0', '-vn', '-ac', '1', '-ar', '16000', '-b:a', '48k', str(target)]
                    result = run_interruptibly(command, input=playlist.encode(), capture_output=True,
                        timeout=max(1, timeout - (time.monotonic() - started)), check=False)
                    if result.returncode or not target.is_file() or target.stat().st_size == 0:
                        raise RuntimeError('media_download_failed')
                    return {'method':'hls_prefetch', 'segments':len(segments), 'concurrency':4,
                            'network_bytes':network_bytes, 'network_seconds':round(network_seconds,3),
                            'decode_seconds':round(time.monotonic()-started-network_seconds,3)}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise RuntimeError('media_download_failed') from None
        timeout = max(1, timeout - (time.monotonic() - started))
    # The URL is transient argv only; ffmpeg's diagnostic stream may echo signed
    # locators, so never propagate it to a result, exception, log or artifact.
    command = [executable, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "http,https,tcp,tls,crypto,httpproxy", "-rw_timeout", "30000000", "-headers", f"User-Agent: Mozilla/5.0\r\nReferer: {referer}\r\n",
               "-i", url, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(target)]
    try:
        result = run_interruptibly(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("media_download_failed") from None
    if result.returncode or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("media_download_failed")
    return {'method':'ffmpeg'}
