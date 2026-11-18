"""Local HTML documents; scripts and external resources are never run."""
from bs4 import BeautifulSoup

from .native_common import html_markdown

FORMATS = {'html', 'htm'}


def parse(d):
    return globals()['parse_' + d.path.suffix.lower().lstrip('.')](d)


def parse_html(d):
    if d.path.stat().st_size > d.limits.max_text_bytes:
        from .base import EngineError
        raise EngineError('document_size_limit')
    soup = BeautifulSoup(d.path.read_text(encoding='utf-8-sig'), 'html.parser')
    for hidden in soup.find_all(['script', 'style', 'noscript', 'iframe', 'object', 'embed']):
        hidden.decompose()
    body = soup.find('article') or soup.find('main') or soup.body or soup
    for image in body.find_all('img'):
        d.gap('external_image_ocr_unavailable')
    d.add(html_markdown(d, str(body)))
    d.meta.update(scripts_executed=False, external_resources_fetched=False)


parse_htm = parse_html


def member(base, href):
    import posixpath
    from urllib.parse import unquote, urlsplit
    from .base import EngineError
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise EngineError('document_external_resource_blocked')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    if target.startswith(('../', '/')) or '\\' in target:
        raise EngineError('document_invalid_container')
    return target


def parse_epub(d):
    from zipfile import ZipFile
    from .native_common import xml, name, attr
    with ZipFile(d.path) as z:
        container = xml(z.read('META-INF/container.xml'))
        package = next(attr(n, 'full-path') for n in container.iter() if name(n) == 'rootfile')
        package = member('', package)
        root = xml(z.read(package))
        book_metadata = {name(n): ''.join(n.itertext()) for section in root if name(section) == 'metadata' for n in section if ''.join(n.itertext()).strip()}
        d.meta['ebook_metadata'] = book_metadata
        manifest = {attr(n, 'id'): n for n in root.iter() if name(n) == 'item'}
        cover_ids = {attr(n, 'content') for n in root.iter() if name(n) == 'meta' and attr(n, 'name') == 'cover'}
        cover_paths = {member(package, attr(n, 'href')) for n in root.iter() if name(n) == 'reference' and attr(n, 'type') == 'cover'}
        d.meta['parts_order'] = []
        for ref in (n for n in root.iter() if name(n) == 'itemref'):
            identifier = attr(ref, 'idref')
            item = manifest.get(identifier)
            if item is None:
                d.gap('epub_missing_spine_item', identifier)
                continue
            path = member(package, attr(item, 'href'))
            if 'nav' in attr(item, 'properties').split() or path in cover_paths or identifier in cover_ids:
                continue

            def include(path=path):
                soup = BeautifulSoup(z.read(path), 'html.parser')
                for node in soup.find_all(['nav', 'script', 'style']):
                    node.decompose()
                body = soup.body or soup
                clone = BeautifulSoup(str(body), 'html.parser')
                for n in clone.find_all(['a', 'h1', 'h2', 'h3', 'img']):
                    n.decompose()
                # Generated link-only TOCs have no prose after links/headings.
                if not clone.get_text(strip=True) and body.find('a'):
                    return
                value = html_markdown(d, str(body))
                if value.strip():
                    d.add(value)
                    d.meta['parts_order'].append(path)
            d.section(path, include)
        for key in ('language', 'date', 'identifier'):
            if book_metadata.get(key):
                d.add('**' + key.title() + ':** ' + book_metadata[key])
        for identifier, item in manifest.items():
            if attr(item, 'media-type').startswith('image/') and identifier not in cover_ids and 'cover-image' not in attr(item, 'properties').split():
                path = member(package, attr(item, 'href'))
                d.section(path, lambda path=path, item=item: d.image(z.read(path), path, attr(item, 'media-type')))


FORMATS.add('epub')


def parse_fb2(d):
    import base64
    from .native_common import xml, name, attr
    root = xml(d.path.read_bytes())
    d.meta['parts_order'] = []

    def inline(node):
        value = node.text or ''
        for child in node:
            body = inline(child)
            if name(child) == 'strong' and body.strip():
                body = '**' + body + '**'
            elif name(child) == 'emphasis' and body.strip():
                body = '*' + body + '*'
            elif name(child) == 'a' and attr(child, 'href'):
                body = '[' + body + '](' + attr(child, 'href') + ')'
            value += body + (child.tail or '')
        return value

    def walk(node, depth=1):
        kind = name(node)
        if kind == 'title':
            value = ' '.join(inline(p) for p in node)
            if value.strip():
                d.add('#' * min(depth, 6) + ' ' + value)
                d.meta['parts_order'].append(value)
        elif kind in {'p','v','subtitle','text-author'}:
            d.add(inline(node))
        elif kind == 'table':
            d.table([[inline(c) for c in row] for row in node if name(row) == 'tr'])
        elif kind != 'image':
            for child in node:
                walk(child, depth+1 if kind == 'section' else depth)
    for body in (n for n in root if name(n) == 'body'):
        d.section(attr(body, 'name') or 'body', lambda body=body: walk(body))
    for binary in (n for n in root if name(n) == 'binary'):
        d.section('image:' + attr(binary, 'id'), lambda binary=binary: d.image(base64.b64decode(''.join(binary.itertext()).strip()), attr(binary, 'id'), attr(binary, 'content-type', 'application/octet-stream')))


FORMATS.add('fb2')
