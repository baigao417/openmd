"""Small authored document containers."""
from zipfile import ZipFile

def authored(path, empty=False, encrypted=False):
    suffix = path.suffix[1:]
    if suffix in {'html', 'htm'}:
        path.write_text('<html><body>' + ('' if empty else '<h1>Original title</h1><p>Original 中文</p><table><tr><td>Name</td><td>Value</td></tr><tr><td>Apple</td><td>7</td></tr></table>') + '</body></html>', encoding='utf-8')
    elif suffix == 'rtf':
        path.write_text(r'{\rtf1\ansi ' + ('' if empty else r'Original title\par Original \u20013?\u25991?\par \trowd Name\cell Value\cell\row\trowd Apple\cell 7\cell\row') + '}', encoding='utf-8')
    elif suffix == 'fb2':
        path.write_text('<FictionBook><body>' + ('' if empty else '<section><title><p>Original title</p></title><p>Original 中文</p><section><title><p>Second</p></title><p>Second original</p></section></section>') + '</body></FictionBook>', encoding='utf-8')
    elif suffix == 'epub':
        with ZipFile(path, 'w') as z:
            z.writestr('mimetype', 'application/epub+zip')
            z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            z.writestr('OPS/book.opf', '<package><manifest><item id="b" href="second.xhtml"/><item id="a" href="first.xhtml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr('OPS/first.xhtml', '<html><body>' + ('' if empty else '<h1>Original title</h1><p>Original 中文</p>') + '</body></html>')
            z.writestr('OPS/second.xhtml', '<html><body>' + ('' if empty else '<h1>Second</h1><p>Second original</p>') + '</body></html>')
            if encrypted:
                z.writestr('META-INF/encryption.xml', '<encryption/>')
    else:
        kind = {'odt':'text', 'ods':'spreadsheet', 'odp':'presentation'}[suffix]
        body = '' if empty else '<text:h text:outline-level="1">Original title</text:h><text:p>Original 中文</text:p>'
        if suffix == 'ods' and not empty:
            body = '<table:table table:name="First"><table:table-row><table:table-cell><text:p>Original 中文</text:p></table:table-cell><table:table-cell><text:p>7</text:p></table:table-cell></table:table-row></table:table>'
        if suffix == 'odp' and not empty:
            body = '<draw:page draw:name="First"><text:p>Original 中文</text:p><presentation:notes><text:p>Speaker original</text:p></presentation:notes></draw:page><draw:page draw:name="Second"><text:p>Second original</text:p></draw:page>'
        with ZipFile(path, 'w') as z:
            z.writestr('mimetype', 'application/vnd.oasis.opendocument.' + kind)
            z.writestr('content.xml', '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"><office:body><office:' + kind + '>' + body + '</office:' + kind + '></office:body></office:document-content>')
            if encrypted:
                z.writestr('META-INF/manifest.xml', '<manifest><encryption-data/></manifest>')
    if encrypted and suffix in {'html', 'htm', 'rtf', 'fb2'}:
        path.write_bytes(b'-----BEGIN PGP MESSAGE-----\nENCRYPTED')
    return path
