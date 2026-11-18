"""Offline OpenCLI dependency inventory and fail-closed static ABI checks.

Never imports adapters or executes JavaScript. CLI column dependencies live in
the bundled declarations; owned adapter columns come from their cli() metadata.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
_TOKEN = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[\w$-]+|[^\s]''')


def tokens(source: str) -> list[str]:
    return [m.group() for m in _TOKEN.finditer(source) if not m.group().startswith(('//', '/*'))]


def _string(value: str) -> str | None:
    if len(value) >= 2 and value[0] in "\"'`" and value[-1] == value[0]:
        return value[1:-1]
    return None


def _end(ts: list[str], start: int) -> int:
    pairs = {'{': '}', '[': ']', '(': ')'}
    stack = [pairs[ts[start]]]
    for i in range(start + 1, len(ts)):
        if ts[i] in pairs:
            stack.append(pairs[ts[i]])
        elif ts[i] in pairs.values():
            if not stack or ts[i] != stack.pop():
                raise ValueError('opencli_metadata_unbalanced')
            if not stack:
                return i
    raise ValueError('opencli_metadata_unbalanced')


def _properties(ts: list[str]) -> dict[str, list[str]]:
    result = {}
    i = 1
    while i < len(ts) - 1:
        if ts[i] == ',':
            i += 1
            continue
        key = _string(ts[i]) or ts[i]
        if i + 1 >= len(ts) or ts[i + 1] != ':':
            raise ValueError('opencli_metadata_dynamic_property')
        j = i + 2
        start = j
        while j < len(ts) - 1 and ts[j] != ',':
            j = _end(ts, j) + 1 if ts[j] in {'{', '[', '('} else j + 1
        result[key] = ts[start:j]
        i = j + 1
    return result


def cli_definitions(source: str) -> list[dict]:
    """Read literal cli({...}) metadata, ignoring comments and function bodies."""
    result = []
    blocks = [(m.start(), m.end()) for m in re.finditer(r'/\*[\s\S]*?\*/', source)]
    pattern = r'(?m)^[ \t]*(?:export\s+)?(?:const\s+\w+\s*=\s*)?cli\s*\(\s*\{'
    for match in re.finditer(pattern, source):
        if any(a <= match.start() < b for a, b in blocks):
            continue
        ts = tokens(source[match.end() - 1:])
        # func is the final field; do not tokenize/interpret arbitrary JS bodies
        # (regular expressions and template interpolation are irrelevant here).
        start = 0
        j = start + 1
        while j < len(ts) and ts[j] != '}':
            if ts[j] == ',':
                j += 1
                continue
            if ts[j] == 'func' or ts[j:j + 2] == ['async', 'func']:
                break
            if ts[j + 1] != ':':
                raise ValueError('opencli_metadata_dynamic_property')
            j += 2
            while j < len(ts) and ts[j] not in {',', '}'}:
                j = _end(ts, j) + 1 if ts[j] in {'{', '[', '('} else j + 1
        props = _properties(ts[start:j] + ['}'])
        def literal(name):
            value = props.get(name, [])
            return _string(value[0]) if len(value) == 1 else None
        args = []
        arg_tokens = props.get('args', [])
        if arg_tokens:
            if arg_tokens[0] != '[' or arg_tokens[-1] != ']':
                raise ValueError('opencli_metadata_dynamic_args')
            j = 1
            while j < len(arg_tokens) - 1:
                if arg_tokens[j] == ',':
                    j += 1
                    continue
                if arg_tokens[j] != '{':
                    raise ValueError('opencli_metadata_dynamic_arg')
                stop = _end(arg_tokens, j)
                arg = _properties(arg_tokens[j:stop + 1])
                name = arg.get('name', [])
                if len(name) != 1 or _string(name[0]) is None:
                    raise ValueError('opencli_metadata_arg_name_invalid')
                args.append({'name': _string(name[0]), 'positional': arg.get('positional') == ['true']})
                j = stop + 1
        columns = props.get('columns', [])
        if columns and (columns[0] != '[' or columns[-1] != ']' or any(_string(t) is None for t in columns[1:-1] if t != ',')):
            raise ValueError('opencli_metadata_dynamic_columns')
        if literal('site') and literal('name'):
            result.append({'site': literal('site'), 'command': literal('name'), 'args': args,
                           'columns': [_string(t) for t in columns[1:-1] if t != ',']})
    return result


def _symbol(node: ast.AST, functions: dict, seen: tuple = ()) -> list[str]:
    if isinstance(node, ast.Constant):
        return [str(node.value)]
    if isinstance(node, (ast.List, ast.Tuple)):
        result = []
        for el in node.elts:
            result += _symbol(el.value if isinstance(el, ast.Starred) else el, functions, seen)
        return result
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions and node.func.id not in seen:
        fn = functions[node.func.id]
        returns = [n.value for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, (ast.List, ast.Tuple))]
        return [v for n in returns for v in _symbol(n, functions, (*seen, node.func.id))]
    return ['{' + ast.unparse(node) + '}']


def inventory(root: Path = ROOT) -> dict:
    declaration = json.loads((root / 'src/openmd/routing/declarations.json').read_text(encoding='utf-8'))
    dependencies = declaration.get('opencli_contracts', {})
    records: dict[tuple[str, str], dict] = {}
    def add(argv, source, *, extra=()):
        if len(argv) < 3 or argv[0] != 'opencli':
            return
        site, command = argv[1:3]
        if site.startswith('{') or command.startswith('{'):
            raise ValueError(f'opencli_inventory_unresolved_command:{source}')
        key = site, command
        row = records.setdefault(key, {'site': site, 'command': command, 'args': [], 'positionals': 0,
                                      'columns': dependencies.get('/'.join(key), []), 'sources': [], 'argv': [], 'kind': 'upstream'})
        argv = list(argv)
        all_args = argv[3:] + list(extra)
        row['args'] = sorted(set(row['args']) | {a for a in all_args if re.fullmatch(r'--?[\w-]+', a)})
        # Every constructed target before the first flag is a positional value.
        count = next((i for i, a in enumerate(argv[3:]) if a.startswith('-')), len(argv) - 3)
        row['positionals'] = max(count, row['positionals'])
        if source not in row['sources']:
            row['sources'].append(source)
        if argv not in row['argv']:
            row['argv'].append(argv)

    paths = sorted((root / 'src/openmd/backends').glob('*.py')) + [root / 'src/openmd/runtime/opencli_adapters.py']
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        sites = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == 'OpenCliAiChatProvider' and n.args and isinstance(n.args[0], ast.Constant)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or len(node.elts) < 3 or not isinstance(node.elts[0], ast.Constant) or node.elts[0].value != 'opencli':
                continue
            argv = _symbol(node, functions)
            # comparisons and registries are not command constructions
            if not argv[2] or argv[2].startswith('{'):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents.get(owner)
            def extras(site):
                result = []
                for call in ast.walk(owner) if owner else []:
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in {'extend', 'append'}):
                        continue
                    parent = parents.get(call)
                    enabled = True
                    while parent is not None and parent is not owner:
                        if isinstance(parent, ast.If) and isinstance(parent.test, ast.Compare) and isinstance(parent.test.left, ast.Name) and parent.test.left.id == 'site':
                            test = parent.test
                            try:
                                value = ast.literal_eval(test.comparators[0])
                                if isinstance(test.ops[0], ast.In):
                                    enabled &= site in value
                                elif isinstance(test.ops[0], ast.Eq):
                                    enabled &= site == value
                            except (ValueError, TypeError):
                                raise ValueError('opencli_inventory_dynamic_site_condition') from None
                        parent = parents.get(parent)
                    if enabled:
                        for arg in call.args:
                            result.extend(_symbol(arg, functions))
                return result
            source = f'{path.relative_to(root).as_posix()}:{node.lineno}'
            if argv[1] == '{site}':
                for site in sorted(sites):
                    add([argv[0], site, *argv[2:]], source, extra=extras(site))
            else:
                add(argv, source, extra=extras(argv[1]))

    for name, card in declaration['cards'].items():
        for cap in card.get('capabilities', []):
            argv = cap.get('argv', [])
            executable = card.get('executable', card.get('command', ''))
            if argv[:1] == ['opencli']:
                add(argv, f'src/openmd/routing/declarations.json:cards/{name}/{cap["id"]}')
            elif executable == 'opencli':
                add(['opencli', *argv], f'src/openmd/routing/declarations.json:cards/{name}/{cap["id"]}')
    for path in sorted((root / 'integrations/opencli').rglob('*')):
        if path.suffix not in {'.ts', '.js'} or path.name.endswith('.test.ts'):
            continue
        for definition in cli_definitions(path.read_text(encoding='utf-8')):
            key = definition['site'], definition['command']
            if key not in records:
                continue  # shipped utilities are not automatically live call sites
            row = records[key]
            row.update(kind='owned', definition=path.relative_to(root).as_posix(), columns=definition['columns'])
            row['declared_args'] = definition['args']
    return {'schema': 'openmd.opencli-inventory/v1', 'commands': [records[k] for k in sorted(records)]}


def _commander_options(source: str) -> set[str]:
    ts = tokens(source)
    return {flag for i, t in enumerate(ts) if (value := _string(t)) is not None and i >= 2
            and ts[i - 1] == '(' and ts[i - 2] in {'option', 'requiredOption', 'Option'}
            for flag in re.findall(r'--?[\w-]+', value)}


def check_contracts(data: dict | None = None, *, package_root: Path | None = None, source_root: Path = ROOT) -> list[str]:
    if package_root is None:
        from .managed_tools import managed_package_root
        package_root = managed_package_root()
    data = data if data is not None else inventory(source_root)
    failures = []
    global_options = set()
    for relative in ('dist/src/cli.js', 'dist/src/commanderAdapter.js'):
        path = package_root / relative
        if path.is_file():
            global_options |= _commander_options(path.read_text(encoding='utf-8'))
    for row in data['commands']:
        label = row['site'] + '/' + row['command']
        if row['site'] == 'browser':
            path = package_root / 'dist/src/commands/browser.js'
            if not path.is_file():
                failures.append(f'{label}:command_missing')
                continue
            source = path.read_text(encoding='utf-8')
            # browser is a built-in Commander namespace, not an adapter cli().
            matches = list(re.finditer(r"\.command\(\s*['\"]([^'\"]+)['\"]\s*\)", source))
            sections = [source[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(source)]
                        for i, m in enumerate(matches) if m.group(1).split()[0] == row['command']]
            if not sections:
                failures.append(f'{label}:command_missing')
                continue
            namespace = next(source[m.start():matches[i + 1].start()] for i, m in enumerate(matches[:-1]) if m.group(1) == 'browser')
            allowed = global_options | _commander_options(namespace) | _commander_options('\n'.join(sections))
            positional = len(re.findall(r'\.argument\(', '\n'.join(sections)))
            columns = []
        else:
            path = source_root / row['definition'] if row.get('kind') == 'owned' else package_root / 'clis' / row['site'] / (row['command'] + '.js')
            if not path.is_file():
                failures.append(f'{label}:command_missing')
                continue
            try:
                definition = next(d for d in cli_definitions(path.read_text(encoding='utf-8')) if d['site'] == row['site'] and d['command'] == row['command'])
            except (ValueError, StopIteration):
                failures.append(f'{label}:metadata_invalid')
                continue
            allowed = global_options | {'--' + a['name'] for a in definition['args']}
            positional = sum(a['positional'] for a in definition['args'])
            columns = definition['columns']
        failures.extend(f'{label}:arg_missing:{arg}' for arg in row['args'] if arg not in allowed)
        failures.extend(f'{label}:column_missing:{col}' for col in row['columns'] if col not in columns)
        if row['positionals'] > positional:
            failures.append(f'{label}:positional_missing')
    return failures


def projections(root: Path, *, source_root: Path = ROOT) -> dict[Path, str]:
    data = inventory(source_root)
    lines = ['# OpenCLI command dependencies', '',
             'Generated statically from call sites, declarations and owned cli() metadata. No commands are executed.',
             'Only required column names are pinned; raw Markdown/file consumers need no table columns. Planned providers are excluded.', '',
             '| Site | Command | Arguments (including optional branches) | Positionals | Required columns | Owner |', '|---|---|---|---|---|---|']
    for row in data['commands']:
        lines.append(f"| {row['site']} | {row['command']} | {' '.join(row['args']) or '—'} | {row['positionals']} | {', '.join(row['columns']) or '—'} | {row['kind']} |")
    lines += ['', '## Source references', '']
    for row in data['commands']:
        lines.append(f"- `{row['site']}/{row['command']}`: " + ', '.join(f'`{s}`' for s in row['sources']))
    return {root / 'docs/opencli-inventory.generated.md': '\n'.join(lines) + '\n',
            root / 'docs/opencli-inventory.generated.json': json.dumps(data, ensure_ascii=False, indent=2) + '\n'}
