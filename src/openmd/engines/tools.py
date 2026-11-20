"""Detected-only document tools, with owned children and bounded output."""
from __future__ import annotations

from dataclasses import replace
import base64
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import time

from openmd.core.local_config import load_local_config
from openmd.runtime.interpreters import owned_popen, process_options, wait_interruptibly
from openmd.runtime.tempdirs import owned_temporary_directory

from .base import DocumentEngine, EngineError, EngineLimits, failure, success
from .sniff import signature, validate_document


def _clean_pandoc_markdown(text: str, title: str) -> str:
    """Normalize Pandoc's document wrapper without preserving raw HTML."""
    from openmd.core.content_validation import html_tag_residue, html_to_markdown

    value = text.strip()
    if html_tag_residue(value)[0]:
        value = html_to_markdown(value)
    lines = value.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("#") and lines[0].lstrip().lstrip("#").strip().casefold() == title.casefold():
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def executable(tool: str) -> str | None:
    if tool == 'soffice':
        from openmd.ocr.office import soffice_path
        selected = soffice_path()
        if selected and os.name == 'nt':
            console = Path(selected).with_suffix('.com')
            if Path(selected).name.lower() == 'soffice.exe' and console.is_file():
                return str(console)
        return selected
    location = load_local_config().get('tools', {}).get(tool, {})
    if not isinstance(location, dict):
        return None
    selected = os.getenv('OPENMD_' + tool.upper().replace('-', '_')) or location.get('executable')
    if selected:
        candidate = Path(selected).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else shutil.which(str(selected))
    found = shutil.which('ebook-convert' if tool == 'calibre' else tool)
    if found:
        return found
    if tool == 'pandoc' and os.name == 'nt':
        candidate = Path(os.getenv('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Pandoc/pandoc.exe'
        if candidate.is_file():
            return str(candidate)
    if tool == 'pandoc':
        # pypandoc-binary bundles a local executable. Never call its download API.
        import importlib.util
        spec = importlib.util.find_spec('pypandoc')
        if spec and spec.origin:
            candidate = Path(spec.origin).parent / 'files' / ('pandoc.exe' if os.name == 'nt' else 'pandoc')
            if candidate.is_file():
                return str(candidate)
    return None


def run_command(argv: list[str], directory: Path, limits: EngineLimits) -> str:
    """No shell, no visible Windows console, no unbounded captured pipes."""
    stdout, stderr = directory / '.engine-stdout', directory / '.engine-stderr'
    def check_output():
        if any(p.exists() and p.stat().st_size > limits.max_output_bytes for p in (stdout, stderr)):
            raise EngineError('document_output_limit')
    try:
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = owned_popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                cwd=directory, shell=False, **process_options())
            code = wait_interruptibly(process, limits.remaining(), on_poll=check_output)
        check_output()
        if code:
            # Tool stderr can contain document body or private paths. Return only
            # its class/exit code, never untrusted process output as a receipt.
            error = stderr.read_bytes()[:65536].lower()
            drm = b'drm' in error or b'encrypted' in error
            raise EngineError('ebook_drm_protected' if drm else 'document_conversion_failed',
                'Encrypted ebook is not accepted.' if drm else f'Document converter exited with code {code}.')
        return stdout.read_text(encoding='utf-8-sig')
    except subprocess.TimeoutExpired as exc:
        raise EngineError('document_timeout') from exc


class ExecutableEngine(DocumentEngine):
    formats: frozenset[str]
    install_hint: str

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        return path.suffix.lower().lstrip('.') in self.formats and signature(path, sniff)

    def available(self):
        command = executable(self.id)
        if not command:
            return {'ok': False, 'reason': self.install_hint, 'version': None}
        try:
            with owned_temporary_directory(prefix='openmd-engine-version-') as owned_root:
                root = Path(owned_root)
                argv = [command, '--version']
                if self.id == 'soffice':
                    argv = [command, '-env:UserInstallation=' + (root / 'profile').as_uri(),
                            '--headless', '--nologo', '--norestore', '--version']
                version = run_command(argv, root, EngineLimits(timeout_s=5))
            version = version.splitlines()[0][:160] if version.strip() else 'unknown'
        except (EngineError, OSError, UnicodeError):
            return {'ok': False, 'reason': f'{self.id} executable cannot be started.', 'version': None}
        return {'ok': True, 'reason': f'{self.id} executable detected.', 'version': version}


class PandocEngine(ExecutableEngine):
    id = 'pandoc'
    formats = frozenset({'epub', 'rtf', 'odt'})
    install_hint = 'Install Pandoc and add it to PATH, or configure tools.pandoc.executable.'

    def convert(self, path: Path, limits: EngineLimits | None = None):
        limits = limits or EngineLimits()
        try:
            sniff = validate_document(path, limits)
            if not self.can_handle(path, sniff):
                return failure(self.id, path, 'document_format_unsupported')
            command = executable(self.id)
            if not command:
                return failure(self.id, path, 'engine_unavailable', self.install_hint)
            with owned_temporary_directory(prefix='openmd-pandoc-') as owned_root:
                root = Path(owned_root)
                # No filters, Lua, citations or user-selected readers are run.
                text = run_command([command, '--sandbox', '--from=' + path.suffix[1:].lower(),
                    '--to=gfm-raw_html',
                    '--wrap=none', str(path.resolve())], root, limits)
                text = _clean_pandoc_markdown(text, path.stem)
            return success(self.id, path, text)
        except (EngineError, OSError, UnicodeError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc)


class SofficeEngine(ExecutableEngine):
    id = 'soffice'
    formats = frozenset({'doc', 'xls', 'ppt', 'rtf', 'odt', 'odp', 'ods'})
    install_hint = 'Install LibreOffice and add soffice to PATH, or configure soffice / OPENMD_SOFFICE.'

    def convert(self, path: Path, limits: EngineLimits | None = None):
        limits = limits or EngineLimits()
        try:
            sniff = validate_document(path, limits)
            if not self.can_handle(path, sniff):
                return failure(self.id, path, 'document_format_unsupported')
            command = executable(self.id)
            if not command:
                return failure(self.id, path, 'engine_unavailable', self.install_hint)
            # Calc/Impress cannot export a meaningful DOCX. Preserve their type
            # by using XLSX/PPTX; Writer inputs become DOCX.
            target = 'xlsx' if path.suffix.lower() in {'.xls', '.ods'} else 'pptx' if path.suffix.lower() in {'.ppt', '.odp'} else 'docx'
            with owned_temporary_directory(prefix='openmd-soffice-') as owned_root:
                root = Path(owned_root)
                # A private profile prevents attachment to the user's running
                # LibreOffice. Disable macro execution and external link updates.
                profile = root / 'profile/user'
                profile.mkdir(parents=True)
                (profile / 'registrymodifications.xcu').write_text(
                    '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
                    '<item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item></oor:items>', encoding='utf-8')
                run_command([command, '-env:UserInstallation=' + (root / 'profile').as_uri(),
                    '--headless', '--nologo', '--norestore', '--nodefault', '--convert-to', target,
                    '--outdir', str(root), str(path.resolve())], root, limits)
                output = root / (path.stem + '.' + target)
                if not output.is_file():
                    raise EngineError('document_conversion_failed', 'LibreOffice produced no converted document.')
                from .registry import convert_document
                result = convert_document(output, limits)
            return replace(result, backend=self.id, title=path.stem,
                metadata={**result.metadata, 'source_format': path.suffix[1:].lower(), 'conversion_target': target})
        except (EngineError, OSError, UnicodeError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc)


class CalibreEngine(ExecutableEngine):
    id = 'calibre'
    formats = frozenset({'mobi', 'azw3', 'fb2'})
    install_hint = 'Install Calibre (ebook-convert), add it to PATH, or configure tools.calibre.executable. DRM removal is not supported.'

    def convert(self, path: Path, limits: EngineLimits | None = None):
        limits = limits or EngineLimits()
        try:
            sniff = validate_document(path, limits)  # DRM check precedes detection
            if not self.can_handle(path, sniff):
                return failure(self.id, path, 'document_format_unsupported')
            command = executable(self.id)
            if not command:
                return failure(self.id, path, 'engine_unavailable', self.install_hint)
            with owned_temporary_directory(prefix='openmd-calibre-') as owned_root:
                root = Path(owned_root)
                output = root / (path.stem + '.epub')
                run_command([command, str(path.resolve()), str(output)], root, limits)
                if not output.is_file():
                    raise EngineError('document_conversion_failed')
                from .registry import convert_document
                result = convert_document(output, limits)
                # Older intermediates can contribute a filename-only heading.
                # Remove that wrapper without changing source chapter order.
                text = re.sub(r'\A\s*(?:#{1,6}\s+converted\s*\n|converted\s*\n[=-]+\s*\n)\s*', '', result.text, count=1, flags=re.I)
                result = replace(result, text=text)
            return replace(result, backend=self.id, title=path.stem,
                metadata={**result.metadata, 'source_format': path.suffix[1:].lower(), 'converted_by': self.id})
        except (EngineError, OSError, UnicodeError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc)


class MinerUEngine(ExecutableEngine):
    """Optional user-installed MinerU CLI. Never installs or downloads models."""
    id = 'mineru'
    formats = frozenset({'pdf', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff'})
    install_hint = 'Install MinerU yourself and configure tools.mineru.executable or OPENMD_MINERU.'

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        suffix = path.suffix.lower()
        if suffix == '.pdf':
            return sniff.startswith(b'%PDF-')
        signatures = {'.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff',
            '.jpeg': b'\xff\xd8\xff', '.bmp': b'BM', '.tif': (b'II*\x00', b'MM\x00*'),
            '.tiff': (b'II*\x00', b'MM\x00*')}
        if suffix == '.webp':
            return sniff.startswith(b'RIFF') and sniff[8:12] == b'WEBP'
        signature = signatures.get(suffix)
        return bool(signature and (sniff.startswith(signature) if isinstance(signature, bytes)
                                   else any(sniff.startswith(value) for value in signature)))

    def available(self):
        command = executable(self.id)
        # MinerU's stable CLI is `mineru`; the benchmark installs mineru-kit.exe
        # as a compatibility launcher for the same `parse` interface.
        if not command:
            command = shutil.which('mineru-kit')
        if not command:
            return {'ok': False, 'reason': self.install_hint, 'version': None}
        try:
            with owned_temporary_directory(prefix='openmd-mineru-version-') as owned_root:
                version = run_command([command, '--version'], Path(owned_root), EngineLimits(timeout_s=5))
            version = version.splitlines()[0][:160] if version.strip() else 'unknown'
        except (EngineError, OSError, UnicodeError):
            return {'ok': False, 'reason': 'MinerU executable cannot be started.', 'version': None}
        return {'ok': True, 'reason': 'User-installed MinerU executable detected.', 'version': version}

    def convert(self, path: Path, limits: EngineLimits | None = None, *, tier: str = 'basic'):
        limits = limits or EngineLimits()
        started = time.monotonic()
        if tier not in {'basic', 'standard'}:
            return failure(self.id, path, 'mineru_setting_invalid')
        try:
            if path.suffix.lower() == '.pdf':
                sniff = validate_document(path, limits)
            else:
                limits.remaining()
                if not path.is_file() or path.stat().st_size > limits.max_file_bytes:
                    raise EngineError('document_local_file_required' if not path.is_file() else 'document_size_limit')
                with path.open('rb') as stream:
                    sniff = stream.read(4096)
            if not self.can_handle(path, sniff):
                return failure(self.id, path, 'document_format_unsupported')
            version_probe = self.available()
            if not version_probe['ok']:
                result = failure(self.id, path, 'engine_unavailable', version_probe['reason'])
                result.metadata['next_action'] = self.install_hint
                return result
            command = executable(self.id) or shutil.which('mineru-kit')
            if not command:
                result = failure(self.id, path, 'engine_unavailable', self.install_hint)
                result.metadata['next_action'] = self.install_hint
                return result
            from openmd.runtime.gpu_lock import gpu_lock
            from openmd.core.models import TranscriptResult
            from openmd.runtime.tempdirs import owned_temporary_directory
            with owned_temporary_directory(prefix='openmd-mineru-') as owned_root:
                root = Path(owned_root)
                output = root / 'output'
                output.mkdir()
                try:
                    with gpu_lock(timeout=limits.remaining()):
                        run_command([command, 'parse', str(path.resolve()), '--output', str(output),
                            '--pages', 'all', '--tier', tier], root, limits)
                except TimeoutError as exc:
                    raise EngineError('mineru_timeout', 'MinerU waited too long for the shared GPU lease.') from exc
                except EngineError as exc:
                    if exc.code == 'document_timeout':
                        raise EngineError('mineru_timeout', 'MinerU exceeded its conversion time limit.') from exc
                    raise EngineError('mineru_failed', 'MinerU conversion failed.') from exc
                markdown = sorted(output.rglob('*.md'))
                if not markdown:
                    raise EngineError('mineru_failed', 'MinerU produced no Markdown output.')
                texts = [p.read_text(encoding='utf-8-sig') for p in markdown]
                text = '\n\n'.join(t.strip() for t in texts if t.strip())
                if not text:
                    raise EngineError('mineru_failed', 'MinerU produced empty Markdown output.')
                image_files = sorted(p.relative_to(output).as_posix() for p in output.rglob('*')
                    if p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'})
                # MinerU Markdown refers to adjacent image files. Inline bounded
                # images so temporary output cleanup cannot leave broken links.
                for image_rel in image_files:
                    image_path = output / Path(image_rel)
                    if image_path.stat().st_size > limits.max_output_bytes:
                        raise EngineError('mineru_failed', 'MinerU image output exceeded the bounded Markdown output size.')
                    mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[image_path.suffix.lower()]
                    uri = f'data:{mime};base64,' + base64.b64encode(image_path.read_bytes()).decode('ascii')
                    image_name = Path(image_rel).name
                    for i, _md_path in enumerate(markdown):
                        texts[i] = re.sub(r'(!\[[^\]]*\]\([^)]*)' + re.escape(image_name) + r'(\))',
                            lambda m: m.group(1) + uri + m.group(2), texts[i])
                text = '\n\n'.join(t.strip() for t in texts if t.strip())
                if len(text.encode('utf-8')) > limits.max_output_bytes:
                    raise EngineError('document_output_limit')
                page_count = max(1, len(texts))
                if path.suffix.lower() == '.pdf':
                    try:
                        import pymupdf
                        with pymupdf.open(path) as document:
                            page_count = len(document)
                    except (ImportError, OSError, ValueError, RuntimeError):
                        pass
                pages = [{'page': i + 1, 'route': 'ocr', 'status': 'verified', 'engine': 'mineru',
                    'model': tier, 'fallback_used': False, 'reason': None,
                    'seconds': None, 'peak_vram_mb': None}
                    for i in range(page_count)]
                return TranscriptResult(text=text, status='verified', source_level='ocr', backend=self.id,
                    title=path.stem, source_notes=['MinerU was invoked as a user-installed local CLI; output requires source comparison.'],
                    metadata={'profile': 'document', 'source_format': path.suffix[1:].lower(),
                        'pages': pages, 'tool_run': {'tool_version': version_probe.get('version'), 'tool_version_verified': bool(version_probe.get('version'))},
                        'images': image_files, 'mineru_tier': tier,
                        'ocr_elapsed_seconds': round(time.monotonic() - started, 4)})
        except EngineError as exc:
            code = 'mineru_timeout' if exc.code in {'document_timeout', 'mineru_timeout'} else exc.code
            result = failure(self.id, path, code if code in {'mineru_timeout', 'mineru_failed', 'engine_unavailable'} else 'mineru_failed', exc)
            if result.error_code == 'engine_unavailable':
                result.metadata['next_action'] = self.install_hint
            return result
        except (OSError, UnicodeError, ValueError) as exc:
            return failure(self.id, path, 'mineru_failed', type(exc).__name__)
        except RuntimeError as exc:
            code = getattr(exc, 'code', 'mineru_failed')
            return failure(self.id, path, code if code in {'mineru_timeout', 'engine_unavailable'} else 'mineru_failed', type(exc).__name__)


class DefuddleEngine(DocumentEngine):
    id = 'defuddle'

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        return path.suffix.lower() in {'.html', '.htm'} and signature(path, sniff)

    def _launcher(self):
        from openmd.routing.loader import load_catalog
        from openmd.routing.runner import resolve_launcher
        return resolve_launcher(load_catalog().cards[self.id])

    def available(self):
        from openmd.routing.runner import ToolError
        try:
            launcher = self._launcher()
            package = launcher.entry_path.parents[1] / 'package.json'
            version = json.loads(package.read_text(encoding='utf-8')).get('version')
            return {'ok': True, 'reason': 'Managed Defuddle Node API detected.', 'version': version}
        except (ToolError, OSError, ValueError, KeyError):
            return {'ok': False, 'reason': 'Configure managed tools.defuddle.root and tools.defuddle.node.', 'version': None}

    def convert(self, path: Path, limits: EngineLimits | None = None):
        limits = limits or EngineLimits()
        try:
            sniff = validate_document(path, limits)
            if not self.can_handle(path, sniff):
                return failure(self.id, path, 'document_format_unsupported')
            if path.stat().st_size > limits.max_text_bytes:
                raise EngineError('document_size_limit')
            launcher = self._launcher()
            module = launcher.entry_path.with_name('node.js')
            # The library's HTML parser does not execute scripts. The local
            # runner also disables fetch/http/https and removes active resources
            # before parsing: no URL is handed to the CLI's network loader.
            script = Path(__file__).with_name('local_html.cjs')
            with owned_temporary_directory(prefix='openmd-defuddle-') as owned_root:
                root = Path(owned_root)
                data = json.loads(run_command([launcher.argv[0], str(script), str(module), str(path.resolve())], root, limits))
            text = data.get('contentMarkdown') or data.get('content') or ''
            return success(self.id, path, text, scripts_executed=False, external_resources_fetched=False)
        except Exception as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc)
