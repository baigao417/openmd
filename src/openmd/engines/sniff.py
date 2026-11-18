"""Bounded signatures and container validation before optional parsers run."""
from __future__ import annotations

import json
import codecs
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .base import EngineError, EngineLimits

LEGACY_FORMATS = frozenset({'pdf', 'docx', 'pptx', 'xlsx'})
MARKITDOWN_FORMATS = LEGACY_FORMATS | {'html', 'htm', 'epub', 'csv', 'tsv', 'json', 'xml', 'ipynb', 'msg'}
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ODF_TYPES = {'odt': 'text', 'odp': 'presentation', 'ods': 'spreadsheet'}
OFFICE_PARTS = {'docx': 'word/document.xml', 'pptx': 'ppt/presentation.xml', 'xlsx': 'xl/workbook.xml'}


def signature(path: Path, sniff: bytes) -> bool:
    suffix = path.suffix.lower().lstrip('.')
    value = sniff.lstrip(b'\xef\xbb\xbf \t\r\n')
    if suffix == 'pdf':
        return b'%PDF-' in sniff[:1024]
    if suffix in OFFICE_PARTS or suffix in ODF_TYPES or suffix in {'epub', 'zip'}:
        return sniff.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'))
    if suffix in {'doc', 'xls', 'ppt', 'msg'}:
        return sniff.startswith(OLE_MAGIC)
    if suffix in {'mobi', 'azw3'}:
        return sniff[60:68] in {b'BOOKMOBI', b'TEXtREAd'}
    if suffix == 'rtf':
        return value.startswith(b'{\\rtf')
    if suffix in {'html', 'htm'}:
        return bool(re.search(rb'<(?:!doctype\s+html|html|head|body|article|h[1-6]|p|div)(?:[\s>])', value, re.I))
    try:
        text = codecs.getincrementaldecoder('utf-8-sig')().decode(value, final=False)
    except UnicodeDecodeError:
        return False
    if '\x00' in text:
        return False
    if suffix in {'xml', 'fb2'}:
        return text.startswith('<')
    if suffix in {'json', 'ipynb'}:
        return bool(text) and text[0] in '[{'  # complete validation below
    if suffix in {'csv', 'tsv'}:
        return bool(text.strip())
    return False


def validate_document(path: Path, limits: EngineLimits) -> bytes:
    limits.remaining()
    if not path.is_file():
        raise EngineError('document_local_file_required', 'An existing local document file is required.')
    input_limit = limits.max_archive_bytes + 16 * 1024 * 1024 if path.suffix.lower() == '.zip' else limits.max_file_bytes
    if path.stat().st_size > input_limit:
        raise EngineError('document_size_limit')
    with path.open('rb') as stream:
        sniff = stream.read(4096)
    suffix = path.suffix.lower().lstrip('.')
    if not signature(path, sniff):
        raise EngineError('archive_invalid_container' if suffix == 'zip' else 'document_invalid_content', 'Suffix and document signature do not match.')
    if suffix in OFFICE_PARTS or suffix in ODF_TYPES or suffix == 'epub':
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                if any(info.flag_bits & 1 for info in archive.infolist()):
                    raise EngineError('ebook_drm_protected' if suffix == 'epub' else 'document_encrypted')
                if (len(archive.infolist()) > limits.max_archive_files
                        or sum(i.file_size for i in archive.infolist()) > limits.max_archive_bytes
                        or any(i.file_size > limits.max_file_bytes or i.file_size / max(i.compress_size, 1) > limits.max_compression_ratio for i in archive.infolist())):
                    raise EngineError('document_invalid_container', 'Document container exceeds resource limits.')
                if suffix in OFFICE_PARTS:
                    valid = {'[Content_Types].xml', OFFICE_PARTS[suffix]} <= names
                else:
                    info = archive.getinfo('mimetype')
                    if info.file_size > 256:
                        raise EngineError('document_invalid_container')
                    mime = archive.read(info).decode('ascii').strip()
                    expected = 'application/epub+zip' if suffix == 'epub' else 'application/vnd.oasis.opendocument.' + ODF_TYPES[suffix]
                    valid = mime == expected and ('META-INF/container.xml' if suffix == 'epub' else 'content.xml') in names
                if not valid:
                    raise EngineError('document_invalid_container')
                if suffix == 'epub' and 'META-INF/encryption.xml' in names:
                    # Fail closed: no decryption or DRM removal is attempted.
                    raise EngineError('ebook_drm_protected', 'Encrypted ebook content is not accepted.')
        except (BadZipFile, KeyError, UnicodeError, OSError) as exc:
            raise EngineError('document_invalid_container') from exc
    if suffix in {'mobi', 'azw3'}:
        with path.open('rb') as stream:
            header = stream.read(86)
            if len(header) < 86 or int.from_bytes(header[76:78], 'big') < 1:
                raise EngineError('document_invalid_content')
            offset = int.from_bytes(header[78:82], 'big')
            if offset < 86 or offset + 16 > path.stat().st_size:
                raise EngineError('document_invalid_content')
            stream.seek(offset)
            record = stream.read(16)
            if int.from_bytes(record[12:14], 'big') != 0:
                raise EngineError('ebook_drm_protected', 'Encrypted MOBI/AZW3 is not accepted; no DRM removal is attempted.')
    if suffix in {'json', 'ipynb', 'xml', 'fb2'}:
        if path.stat().st_size > limits.max_text_bytes:
            raise EngineError('document_size_limit')
        try:
            text = path.read_text(encoding='utf-8-sig')
            if suffix in {'json', 'ipynb'}:
                value = json.loads(text)
                if suffix == 'ipynb' and (not isinstance(value, dict) or not isinstance(value.get('cells'), list) or not isinstance(value.get('nbformat'), int)):
                    raise ValueError('Notebook structure missing')
            else:
                if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
                    raise ValueError('DTD and entities are disabled')
                root = ElementTree.fromstring(text)
                if suffix == 'fb2' and root.tag.rsplit('}', 1)[-1] != 'FictionBook':
                    raise ValueError('FictionBook root missing')
        except (ValueError, UnicodeError, ElementTree.ParseError) as exc:
            raise EngineError('document_invalid_content') from exc
    return sniff
