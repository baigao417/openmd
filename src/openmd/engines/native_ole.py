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
