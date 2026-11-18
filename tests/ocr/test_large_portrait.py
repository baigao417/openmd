"""Large portrait pages retain legible Ovis inputs without changing other crops."""
import math

import pytest
from PIL import Image

from openmd.ocr.config import settings


def test_stream_sends_legible_large_portrait_pixels():
    import base64
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import io
    import json
    import threading
    import time
    from openmd.ocr.ollama import stream_recognize

    sizes = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            encoded = payload['messages'][0]['images'][0]
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                sizes.append(image.size)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'message': {'content': 'source'}, 'done': True, 'done_reason': 'stop'}).encode() + b'\n')

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {**settings(), 'ollama_url': f'http://127.0.0.1:{server.server_port}'}
            result = stream_recognize(Image.new('RGB', (2754, 3939), 'white'), model='ovis3-bf16',
                                      prompt='raw OCR', config=config, deadline=time.monotonic() + 10)
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert result['breaker'] is None
    assert sizes == [(1675, 2396)]
    assert result['input_size'] == [1675, 2396]


@pytest.mark.parametrize('size,model,cap', [
    ((2754, 3939), 'ovis3-bf16', 4014080),
    ((1653, 2339), 'ovis3-bf16', 1003520),
    ((5334, 3000), 'ovis3-bf16', 1003520),
    ((2754, 3939), 'minicpm-ocr-f16', 1003520),
])
def test_input_pixel_policy_uses_only_image_geometry(size, model, cap):
    from openmd.ocr.ollama import prepare_image

    config = settings()
    image = Image.new('RGB', size, 'white')
    result = prepare_image(image, model=model, config=config)
    factor = min(1, math.sqrt(cap / (size[0] * size[1])))
    assert result.size == tuple(max(1, int(side * factor)) for side in size)
    assert image.size == size


def test_local_pixel_override_is_not_reduced_by_portrait_policy():
    from openmd.ocr.ollama import prepare_image

    config = {**settings(), 'max_pixels': 8028160}
    source = Image.new('RGB', (2754, 3939), 'white')
    result = prepare_image(source, model='ovis3-bf16:latest', config=config)
    assert result.width * result.height > 4014080
    assert result.width * result.height <= config['max_pixels']


def test_legacy_explicit_config_keeps_original_pixel_cap():
    from openmd.ocr.ollama import prepare_image

    config = settings()
    config.pop('large_portrait_max_pixels', None)
    source = Image.new('RGB', (1800, 2700), 'white')
    result = prepare_image(source, model='ovis3-bf16', config=config)
    assert result.width * result.height <= config['max_pixels']


def test_large_portrait_cap_is_an_integer_local_setting(tmp_path, monkeypatch):
    import json

    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'schema': 'openmd.local/v1', 'ocr': {'large_portrait_max_pixels': 4014080.5}}))
    monkeypatch.setenv('OPENMD_CONFIG', str(path))
    with pytest.raises(ValueError, match='ocr_setting_integer_required: large_portrait_max_pixels'):
        settings()


def test_capacity_probe_separates_columns_from_a_continuous_figure():
    from PIL import ImageDraw
    from openmd.ocr.breakers import estimate_expected_chars, breaker_reason

    source = Image.new('RGB', (2400, 3600), 'white')
    draw = ImageDraw.Draw(source)
    draw.rectangle((1000, 0, 1400, 3599), fill='black')
    for y in range(120, 3400, 55):
        for left in (140, 1540):
            for x in range(left, left + 680, 30):
                draw.rectangle((x, y, x + 14, y + 22), fill='black')
    base = estimate_expected_chars(source, settings())
    columns = estimate_expected_chars(source, settings(), columns=True)
    assert columns['estimated_chars'] > base['estimated_chars'] * 2
    assert len(columns['column_ink_rows']) == 8
    assert columns['max_length_chars'] == int(columns['estimated_chars'] * settings()['length_multiplier'])
    repeated = 'A sufficiently long source sentence repeats here.\n' * 3
    assert breaker_reason(repeated, columns, settings())['rule'] == 'repeat_sentence'
