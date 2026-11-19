"""Bounded RTF text reader. No OLE objects, scripts or external resources run."""
import re

from .base import EngineError


def parse(d):
    raw = d.path.read_bytes().decode('latin-1')
    states, ignored, uc, encoding, fallback = [], False, 1, 'cp1252', 0
    output = []
    cursor = 0
    skip = {'fonttbl','colortbl','stylesheet','info','listtable','listoverridetable','generator','pict','object','objdata','fldinst','datastore','xmlnstbl','themedata','colorschememapping'}
    while cursor < len(raw):
        d.limits.remaining()
        char = raw[cursor]
        cursor += 1
        if char == '{':
            states.append((ignored, uc, encoding))
        elif char == '}':
            if not states:
                raise EngineError('document_invalid_content')
            ignored, uc, encoding = states.pop()
        elif char == '\\':
            match = re.match(r'([A-Za-z]+)(-?\d+)? ?', raw[cursor:])
            if match:
                cursor += match.end()
                word, number = match.group(1), match.group(2)
                number = int(number) if number is not None else None
                if word in skip:
                    ignored = True
                    if word in {'pict','object'}:
                        d.gap('rtf_embedded_object_unconverted')
                elif word == 'bin':
                    if number is None or number < 0 or cursor + number > len(raw):
                        raise EngineError('document_invalid_content')
                    cursor += number
                elif word == 'uc':
                    uc = max(0, min(number or 0, 16))
                elif word == 'ansicpg':
                    encoding = 'cp' + str(number)
                elif not ignored:
                    if word == 'u':
                        output.append(chr((number or 0) & 0xffff))
                        fallback = uc
                    elif word in {'par','line'}:
                        output.append('\n')
                    elif word == 'trowd':
                        output.append('\x1d')
                    elif word == 'cell':
                        output.append('\x1e')
                    elif word == 'row':
                        output.append('\x1f')
                    elif word == 'outlinelevel':
                        output.append('#' * min(max((number or 0) + 1, 1), 6) + ' ')
                    elif word == 'tab':
                        output.append('\t')
                    elif word in {'emdash','endash','bullet','lquote','rquote','ldblquote','rdblquote'}:
                        output.append({'emdash':'—','endash':'–','bullet':'•','lquote':'‘','rquote':'’','ldblquote':'“','rdblquote':'”'}[word])
            elif cursor < len(raw):
                symbol = raw[cursor]
                cursor += 1
                value = ''
                if symbol == "'":
                    encoded = bytearray()
                    while True:
                        encoded.append(int(raw[cursor:cursor+2], 16))
                        cursor += 2
                        if raw[cursor:cursor+2] != "\\'":
                            break
                        cursor += 2
                    value = encoded.decode(encoding)
                elif symbol == '*':
                    ignored = True
                elif symbol in '{}\\':
                    value = symbol
                elif symbol == '~':
                    value = '\u00a0'
                if not ignored:
                    take = min(fallback, len(value))
                    fallback -= take
                    output.append(value[take:])
        elif not ignored and char not in '\r\n':
            if fallback:
                fallback -= 1
            else:
                output.append(char.encode('latin-1').decode(encoding))
    if states:
        raise EngineError('document_invalid_content')
    text = ''.join(output).encode('utf-16le', 'surrogatepass').decode('utf-16le')
    pending = []
    cursor = 0
    for row in re.finditer(r'\x1d(.*?)\x1f', text, re.S):
        between = text[cursor:row.start()]
        if between.strip():
            if pending:
                d.table(pending)
                pending = []
            d.add(between)
        cells = row.group(1).split('\x1e')
        if not cells[-1].strip():
            cells.pop()
        pending.append([c.strip() for c in cells])
        cursor = row.end()
    if pending:
        d.table(pending)
    d.add(text[cursor:])
    links = list(dict.fromkeys(re.findall(r'HYPERLINK\s+"([^"]+)"', raw, re.I)))
    if links:
        d.add('### Links\n\n' + '\n'.join('- <' + link + '>' for link in links))
