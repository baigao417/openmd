"""Openmd MSG/OLE text glue. Only named local streams are read."""
import re

from .base import EngineError
from .native_common import html_markdown


def stream(ole, path, limits):
    size = ole.get_size(path)
    if size > min(limits.max_file_bytes, limits.max_text_bytes):
        raise EngineError('document_size_limit')
    limits.remaining()
    return ole.openstream(path).read()


def parse_msg(d):
    import olefile
    with olefile.OleFileIO(d.path) as ole:
        names = {p[-1].upper(): p for p in ole.listdir(streams=True, storages=False) if len(p) == 1}

        def prop(identifier):
            for suffix, encoding in [('001F','utf-16le'), ('001E','cp1252')]:
                part = names.get('__SUBSTG1.0_' + identifier + suffix)
                if part:
                    return stream(ole, part, d.limits).decode(encoding).rstrip('\0')
            return ''

        message_class = prop('001A').lower()
        if 'smime' in message_class or 'encrypted' in message_class:
            raise EngineError('document_encrypted')
        body = prop('1000')
        if not body.strip():
            html = names.get('__SUBSTG1.0_10130102')
            if html:
                data = stream(ole, html, d.limits)
                body = html_markdown(d, data.decode('utf-8-sig'))
        if not body.strip():
            raise EngineError('document_empty_text')
        subject, sender, recipient = prop('0037'), prop('0C1F') or prop('0C1A'), prop('0E04')
        if subject:
            d.add('# Email Message\n\n' + subject)
        if sender:
            d.add('**From:** ' + sender)
        if recipient:
            d.add('**To:** ' + recipient)
        if subject:
            d.add('**Subject:** ' + subject)
        d.add('## Content\n\n' + body)
        attachments = [p for p in ole.listdir(storages=True, streams=False) if any(s.startswith('__attach_version1.0_') for s in p)]
        if attachments:
            d.meta['attachments'] = len(attachments)
            d.gap('msg_attachments_not_converted')


def parse_doc(d):
    """Read Fib/Clx/PlcPcd per Microsoft's public MS-DOC structures.

    Only the main text story and basic cell delimiters are supported. Complex
    layout, revision selection, drawing text and header stories are not certified.
    """
    import struct
    import olefile
    with olefile.OleFileIO(d.path) as ole:
        word = stream(ole, 'WordDocument', d.limits)
        if len(word) < 154 or struct.unpack_from('<H', word)[0] != 0xa5ec:
            raise EngineError('document_invalid_content')
        flags = struct.unpack_from('<H', word, 10)[0]
        if flags & 0x8100:
            raise EngineError('document_encrypted')
        # FibRgW and FibRgLw have length fields; never assume a producer's size.
        csw = struct.unpack_from('<H', word, 32)[0]
        cslw_offset = 34 + csw*2
        cslw = struct.unpack_from('<H', word, cslw_offset)[0]
        lw = cslw_offset + 2
        if cslw < 4:
            raise EngineError('document_invalid_content')
        expected = struct.unpack_from('<I', word, lw+12)[0]
        fc_array_count = lw + cslw*4
        count = struct.unpack_from('<H', word, fc_array_count)[0]
        pieces = []
        if count >= 34:
            fc, length = struct.unpack_from('<II', word, fc_array_count+2+33*8)
            table = stream(ole, '1Table' if flags & 0x0200 else '0Table', d.limits)
            clx = table[fc:fc+length]
            cursor = 0
            while cursor < len(clx) and clx[cursor] == 1:
                size = struct.unpack_from('<H', clx, cursor+1)[0]
                cursor += 3 + size
            if cursor < len(clx) and clx[cursor] == 2:
                size = struct.unpack_from('<I', clx, cursor+1)[0]
                plc = clx[cursor+5:cursor+5+size]
                if len(plc) != size or size < 4 or (size-4) % 12:
                    raise EngineError('document_invalid_content')
                n = (size-4)//12
                cps = struct.unpack_from('<' + 'I'*(n+1), plc)
                for index in range(n):
                    d.limits.remaining()
                    begin, end = cps[index:index+2]
                    if begin >= expected:
                        break
                    end = min(end, expected)
                    if end < begin:
                        raise EngineError('document_invalid_content')
                    fc_compressed = struct.unpack_from('<I', plc, 4*(n+1)+index*8+2)[0]
                    compressed = bool(fc_compressed & 0x40000000)
                    offset = fc_compressed & 0x3fffffff
                    width = 1 if compressed else 2
                    if compressed:
                        offset //= 2
                    data = word[offset:offset+(end-begin)*width]
                    if len(data) != (end-begin)*width:
                        raise EngineError('document_invalid_content')
                    pieces.append(data.decode('cp1252' if compressed else 'utf-16le'))
        if not pieces and expected:
            begin, end = struct.unpack_from('<II', word, 24)
            data = word[begin:end]
            pieces.append(data.decode('utf-16le' if len(data) == expected*2 else 'cp1252'))
        original = ''.join(pieces)
        d.meta['text_character_coverage'] = min(1, len(original.encode('utf-16le')) / max(1, expected*2))
        # Fields store instructions separately from displayed results.
        value = re.sub('\x13[^\x14\x15]*(?:\x14([^\x15]*))?\x15', lambda m: m.group(1) or '', original)
        value = re.sub(r'[\x00-\x06\x08\x0b\x0e-\x12\x16-\x1f]', '', value)
        rows = []
        for paragraph in value.split('\r'):
            if '\x07' in paragraph:
                values = paragraph.split('\x07')
                while values and not values[-1]:
                    values.pop()
                if values:
                    rows.append(values)
            else:
                if rows:
                    d.table(rows)
                    rows = []
                d.add(paragraph)
        if rows:
            d.table(rows)
        if value.strip():
            d.gap('legacy_doc_layout_unverified')
            d.meta['text_recall_certified_95'] = False
            d.meta['next_action'] = '安装 LibreOffice 可获得完整转换'
            d.gaps.append('安装 LibreOffice 可获得完整转换')


def parse_ppt(d):
    """MS-PPT RecordHeader, TextCharsAtom and TextBytesAtom text reader.

    Binary persistence records can retain old slides and master content; this
    bounded reader cannot certify active-slide order and always reports partial.
    """
    import struct
    import olefile
    with olefile.OleFileIO(d.path) as ole:
        entries = ole.listdir()
        if any(any('encrypt' in s.lower() for s in p) for p in entries):
            raise EngineError('document_encrypted')
        if ole.exists('Current User'):
            current = stream(ole, 'Current User', d.limits)
            if len(current) >= 16 and struct.unpack_from('<I', current, 12)[0] == 0xf3d1c4df:
                raise EngineError('document_encrypted')
        data = stream(ole, 'PowerPoint Document', d.limits)
        d.meta['parts_order'] = []
        seen = set()

        def records(begin, end, depth=0, owner='document'):
            if depth > 64:
                raise EngineError('document_invalid_content')
            cursor = begin
            while cursor + 8 <= end:
                d.limits.remaining()
                options, kind, length = struct.unpack_from('<HHI', data, cursor)
                content, stop = cursor+8, cursor+8+length
                if options == 0 and kind == 0 and length == 0:
                    break
                if stop > end:
                    raise EngineError('document_invalid_content')
                if kind in {1006, 1008}:
                    label = ('Slide ' if kind == 1006 else 'Notes ') + str(len(d.meta['parts_order'])+1)
                    d.meta['parts_order'].append(label)
                    records(content, stop, depth+1, label)
                elif options & 15 == 15:
                    records(content, stop, depth+1, owner)
                elif kind in {4000, 4008}:
                    value = data[content:stop].decode('utf-16le' if kind == 4000 else 'cp1252').strip('\x00\r\n ')
                    if value and value not in seen:
                        seen.add(value)
                        d.add(value.replace('\r', '\n'))
                cursor = stop
        records(0, len(data))
        if d.parts:
            d.gap('legacy_ppt_order_layout_unverified')
            d.meta['text_recall_certified_95'] = False
            d.meta['next_action'] = '安装 LibreOffice 可获得完整转换'
            d.gaps.append('安装 LibreOffice 可获得完整转换')
