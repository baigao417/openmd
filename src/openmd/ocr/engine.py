"""Local OCR profiles implementing the document engine contract.

No hub, service startup or model download. VLM profiles retain the existing
GPU lease/quarantine lifecycle; the CPU baseline also takes the shared lease.
"""
from __future__ import annotations

from dataclasses import replace
import importlib.metadata
import importlib.util
from pathlib import Path
import time

from openmd.core.models import ExtractionComponent
from openmd.engines.base import DocumentEngine, EngineError, EngineLimits, failure

PROFILES = ('ocr.chain', 'ocr.ovis3', 'ocr.minicpm', 'ocr.rapidocr')


class OcrEngine(DocumentEngine):
    def __init__(self, profile: str | None = None):
        from .config import settings
        self.id = profile or settings()['engine']
        if self.id not in PROFILES:
            raise ValueError('ocr_engine_unknown')
        self._rapid = None

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        return path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'} and (
            sniff.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'BM', b'II*\x00', b'MM\x00*'))
            or (sniff.startswith(b'RIFF') and sniff[8:12] == b'WEBP'))

    def available(self):
        if self.id == 'ocr.rapidocr':
            if importlib.util.find_spec('rapidocr_onnxruntime') is not None:
                return {'ok': True, 'reason': 'Local CPU ONNX; bundled weights only.', 'version': importlib.metadata.version('rapidocr-onnxruntime')}
            from .config import layout_runtime
            python, _ = layout_runtime()
            # This is a runtime candidate, not a claim that its dependencies exist.
            return {'ok': python is not None, 'reason': 'Configured OCR worker; dependency checked during conversion.' if python else 'Install optional openmd-cli[ocr] in the selected local OCR runtime.', 'version': None}
        from .config import settings
        from .ollama import probe_ollama
        probe = probe_ollama(settings())
        keys = ('ovis_model', 'minicpm_model') if self.id == 'ocr.chain' else ('ovis_model',) if self.id == 'ocr.ovis3' else ('minicpm_model',)
        ready = all(probe['models'].get(key, {}).get('available') for key in keys)
        return {'ok': ready, 'reason': 'Existing loopback Ollama models.' if ready else probe.get('error_code', 'ollama_model_missing'), 'version': probe.get('version')}

    def convert(self, path: Path, limits: EngineLimits | None = None):
        path, limits = Path(path), limits or EngineLimits()
        started = time.monotonic()
        if limits.deadline is None:
            limits = replace(limits, deadline=started + limits.timeout_s)
        try:
            limits.remaining()
            if path.stat().st_size > limits.max_file_bytes:
                raise EngineError('document_size_limit')
            with path.open('rb') as stream:
                if not self.can_handle(path, stream.read(32)):
                    raise EngineError('ocr_image_invalid')
            if self.id == 'ocr.rapidocr' and importlib.util.find_spec('rapidocr_onnxruntime') is None:
                from .config import layout_runtime
                python, _ = layout_runtime()
                if not python:
                    raise EngineError('ocr_backend_unavailable')
                from openmd.engines.worker import convert_in_worker
                from openmd.core.models import TranscriptResult
                value = convert_in_worker(path, limits, mode='ocr', python=python, ocr_engine=self.id)
                value['components'] = [ExtractionComponent(**c) for c in value.get('components', [])]
                return TranscriptResult(**value)
            from PIL import Image
            with Image.open(path) as image:
                image.load()
                result = self.recognize_image(image.convert('RGB'), title=path.stem, timeout_s=limits.remaining())
            if len(result.text.encode('utf-8')) > limits.max_output_bytes:
                raise EngineError('document_output_limit')
            return replace(result, metadata={**result.metadata, 'ocr_elapsed_seconds': round(time.monotonic() - started, 6)})
        except (EngineError, OSError, ValueError, ImportError, TimeoutError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'ocr_extraction_failed'), type(exc).__name__)

    def recognize_image(self, image, *, title: str, timeout_s: float | None = None):
        from .config import settings
        config = settings()
        if self.id == 'ocr.rapidocr':
            import numpy as np
            from openmd.runtime.gpu_lock import gpu_lock
            from openmd.adapters.screenshot.backends.rapidocr import _extract_lines, _layout_evidence, _average
            from openmd.core.models import TranscriptResult
            with gpu_lock(timeout=min(config['gpu_lock_seconds'], timeout_s or config['gpu_lock_seconds'])):
                if self._rapid is None:
                    from rapidocr_onnxruntime import RapidOCR
                    self._rapid = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
                rows, _ = self._rapid(np.asarray(image))
            lines, confidence = _extract_lines(rows)
            boxes, ambiguous = _layout_evidence(rows)
            if not lines:
                return failure(self.id, Path(title), 'ocr_empty_text')
            result = TranscriptResult(text='\n'.join(lines), status='partial' if ambiguous else 'verified', source_level='ocr', backend=self.id, title=title,
                source_notes=['Local CPU RapidOCR. Reading order and transcription require source comparison.'],
                metadata={'source_map': boxes, 'average_confidence': _average(confidence), 'reading_order': 'ambiguous' if ambiguous else 'engine_order_not_independently_verified'})
        else:
            from .pipeline import LocalOCRBackend
            result = LocalOCRBackend(self.id).recognize_image(image, title=title, timeout_s=timeout_s)
        blocks = [{'id': 'ocr-1', 'kind': 'text', 'text': result.text, 'source_level': 'ocr'}] if result.text else []
        component = ExtractionComponent(id='ocr-image', kind='image', status=result.status, source_level='ocr', backend=self.id, error_code=result.error_code)
        return replace(result, components=[*result.components, component], metadata={**result.metadata,
            'ocr_engine': {'id': self.id, 'settings': {key: config[key] for key in ('ovis_model', 'minicpm_model', 'repeat_min_chars', 'repeat_count', 'length_multiplier', 'num_predict', 'page_seconds')}},
            'ocr_blocks': blocks})
