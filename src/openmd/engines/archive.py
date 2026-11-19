"""Openmd ZIP container: bounded extraction, detector routing, entry receipts."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from zipfile import BadZipFile, ZipFile

from openmd.core.detector import detect_platform
from openmd.core.models import ExtractionComponent, TranscriptResult
from openmd.runtime.tempdirs import owned_temporary_directory

from .base import DocumentEngine, EngineError, EngineLimits, failure
from .sniff import signature


def safe_member(name: str, root: Path) -> Path:
    normalized = name.replace('\\', '/')
    parts = PurePosixPath(normalized).parts
    if (not parts or normalized.startswith('/') or any(ord(c) < 32 for c in name)
            or any(p in {'.', '..'} or ':' in p or p.endswith((' ', '.')) for p in parts)
            or any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)):
        raise EngineError('archive_unsafe_path', 'Archive contains an unsafe path.')
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise EngineError('archive_unsafe_path')
    return target


def check_directory_size(path: Path, limits: EngineLimits) -> None:
    """Bound central-directory allocation before ZipFile loads all entries."""
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65557))
        tail = stream.read(65557)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise EngineError('archive_invalid_container')
    count = struct.unpack_from('<H', tail, offset + 10)[0]
    directory_size = struct.unpack_from('<I', tail, offset + 12)[0]
    # ZIP64's sentinel exceeds our 1000-entry contract and is refused rather
    # than permitting an unbounded central directory allocation.
    if count + limits.budget.files > limits.max_archive_files or directory_size > 8 * 1024 * 1024:
        raise EngineError('archive_file_limit')


class ZipEngine(DocumentEngine):
    id = 'openmd.zip'

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        return path.suffix.lower() == '.zip' and signature(path, sniff)

    def available(self):
        return {'ok': True, 'reason': 'Openmd bounded ZIP container uses the Python standard library.', 'version': '1'}

    def convert(self, path: Path, limits: EngineLimits | None = None):
        limits = limits or EngineLimits()
        try:
            limits.remaining()
            if limits.archive_depth >= limits.max_archive_depth:
                raise EngineError('archive_depth_limit')
            check_directory_size(path, limits)
            receipts, bodies, components = [], [], []
            output_bytes = 0
            with owned_temporary_directory(prefix='openmd-zip-') as owned_root, ZipFile(path) as archive:
                # Resolved like safe_member's targets: on macOS /var is a symlink to /private/var.
                root = Path(owned_root).resolve()
                infos = archive.infolist()
                if not infos:
                    raise EngineError('archive_empty')
                # Validate the entire central directory before writing any file.
                if len(infos) + limits.budget.files > limits.max_archive_files:
                    raise EngineError('archive_file_limit')
                targets = []
                seen = set()
                declared_bytes = 0
                for info in infos:
                    limits.remaining()
                    target = safe_member(info.filename, root)
                    identity = target.relative_to(root).as_posix().casefold()
                    if identity in seen:
                        raise EngineError('archive_duplicate_path')
                    seen.add(identity)
                    mode = info.external_attr >> 16
                    if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise EngineError('archive_unsafe_path', 'Archive links and special files are not accepted.')
                    if info.flag_bits & 1:
                        raise EngineError('archive_encrypted', 'Encrypted ZIP entries are not accepted.')
                    if info.file_size > limits.max_file_bytes:
                        raise EngineError('archive_file_size_limit')
                    if info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio:
                        raise EngineError('archive_compression_ratio')
                    declared_bytes += info.file_size
                    if declared_bytes + limits.budget.bytes > limits.max_archive_bytes:
                        raise EngineError('archive_size_limit')
                    targets.append((info, target))
                limits.budget.files += len(infos)
                for info, target in targets:
                    limits.remaining()
                    if info.is_dir():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with archive.open(info) as source, target.open('xb') as out:
                        while chunk := source.read(64 * 1024):
                            limits.remaining()
                            written += len(chunk)
                            limits.budget.bytes += len(chunk)
                            if written > limits.max_file_bytes:
                                raise EngineError('archive_file_size_limit')
                            if limits.budget.bytes > limits.max_archive_bytes:
                                raise EngineError('archive_size_limit')
                            out.write(chunk)
                    result, platform = self._route_entry(target, replace(limits, archive_depth=limits.archive_depth + 1))
                    receipt = {'path': info.filename, 'platform': platform, 'status': result.status,
                        'backend': result.backend, 'source_level': result.source_level, 'error_code': result.error_code,
                        'selected_engine': result.metadata.get('selected_engine'),
                        'engine_attempts': result.metadata.get('engine_attempts', [])}
                    for field in ('coverage', 'archive_entries'):
                        if field in result.metadata:
                            receipt['entries' if field == 'archive_entries' else field] = result.metadata[field]
                    receipts.append(receipt)
                    component_id = f'entry-{len(receipts)}'
                    components.append(ExtractionComponent(id=component_id, kind='document' if platform == 'pdf' else 'media' if platform == 'generic_media' else 'text',
                        status=result.status, source_level=result.source_level, backend=result.backend,
                        text_ref='index.md#' + component_id, error_code=result.error_code,
                        warnings=result.source_notes, terminal_reason=result.error_code))
                    if result.text.strip():
                        body = f'<a id="{component_id}"></a>\n\n## {info.filename}\n\n{result.text}'
                        output_bytes += len(body.encode('utf-8'))
                        if output_bytes > limits.max_output_bytes:
                            raise EngineError('document_output_limit')
                        bodies.append(body)
            if not receipts:
                raise EngineError('archive_empty')
            status = 'verified' if all(r['status'] == 'verified' for r in receipts) else 'partial'
            rows = [f"| {r['path'].replace('|', '&#124;')} | {r['platform']} | {r['status']} | {r['error_code'] or '—'} |" for r in receipts]
            text = '# Archive contents\n\n| Entry | Route | Status | Failure / gate |\n|---|---|---|---|\n' + '\n'.join(rows) + '\n\n' + '\n\n'.join(bodies)
            if len(text.encode('utf-8')) > limits.max_output_bytes:
                raise EngineError('document_output_limit')
            return TranscriptResult(text=text, status=status,
                source_level='native_text' if status == 'verified' else 'partial_extraction',
                backend=self.id, title=path.stem, components=components,
                source_notes=['Archive entries routed individually; media requires Openmd ASR. No cloud transcription was invoked.'],
                metadata={'profile': 'document', 'source_format': 'zip', 'archive_entries': receipts,
                    'coverage': {'files_total': len(receipts), 'files_verified': sum(r['status'] == 'verified' for r in receipts),
                        'files_with_gaps': sum(r['status'] != 'verified' for r in receipts),
                        'expected_count': len(receipts), 'fetched_count': sum(r['status'] == 'verified' for r in receipts),
                        'missing_count': sum(r['status'] != 'verified' for r in receipts), 'complete': status == 'verified'},
                    'archive_bytes_expanded': limits.budget.bytes, 'archive_files_counted': limits.budget.files})
        except (EngineError, BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'archive_invalid_container'), exc)

    def _route_entry(self, path: Path, limits: EngineLimits):
        platform = detect_platform(str(path))
        if path.suffix.lower() in {'.ogg', '.oga', '.opus', '.aac', '.wma', '.aif', '.aiff', '.avi', '.mpeg', '.mpg', '.ts'}:
            platform = 'generic_media'
        if platform == 'generic_media':
            return failure('openmd.media', path, 'asr_required', 'Needs Openmd ASR; no media was passed to MarkItDown or uploaded.'), platform
        if platform == 'screenshot':
            return failure('openmd.ocr', path, 'ocr_required', 'Image entry requires the explicitly enabled Openmd OCR route.'), platform
        if platform not in {'pdf', 'qq_group'}:
            return failure(self.id, path, 'archive_entry_unsupported'), platform
        if path.suffix.lower() == '.zip':
            from .registry import convert_document
            return convert_document(path, limits), platform
        from .worker import convert_in_worker
        from .sniff import LEGACY_FORMATS
        try:
            mode = 'qq_export' if platform == 'qq_group' else 'legacy_pipeline' if path.suffix[1:].lower() in LEGACY_FORMATS else 'document'
            result = convert_in_worker(path, limits, mode=mode)
            result['components'] = [ExtractionComponent(**c) for c in result.get('components', [])]
            return TranscriptResult(**result), platform
        except (EngineError, OSError, ValueError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc), platform
