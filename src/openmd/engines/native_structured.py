"""Local HTML documents; scripts and external resources are never run."""
from bs4 import BeautifulSoup

from .native_common import html_markdown

FORMATS = {'html'}


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

