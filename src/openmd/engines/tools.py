"""Detected-only document tools, with owned children and bounded output."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess

from openmd.core.local_config import load_local_config
from openmd.runtime.interpreters import owned_popen, process_options, wait_interruptibly
from openmd.runtime.tempdirs import owned_temporary_directory

from .base import DocumentEngine, EngineError, EngineLimits, failure, success
from .sniff import signature, validate_document


def executable(tool: str) -> str | None:
    if tool == 'soffice':
        from openmd.ocr.office import soffice_path
        return soffice_path()
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
                version = run_command([command, '--version'], root, EngineLimits(timeout_s=5))
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
                    '--to=gfm', '--wrap=none', str(path.resolve())], root, limits)
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
                    '--headless', '--norestore', '--nodefault', '--convert-to', target,
                    '--outdir', str(root), str(path.resolve())], root, limits)
                output = root / (path.stem + '.' + target)
                if not output.is_file():
                    raise EngineError('document_conversion_failed', 'LibreOffice produced no converted document.')
                from .markitdown import MarkItDownEngine
                result = MarkItDownEngine().convert(output, limits)
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
                output = root / 'converted.epub'
                run_command([command, str(path.resolve()), str(output)], root, limits)
                if not output.is_file():
                    raise EngineError('document_conversion_failed')
                from .registry import convert_document
                result = convert_document(output, limits)
            return replace(result, backend=self.id, title=path.stem,
                metadata={**result.metadata, 'source_format': path.suffix[1:].lower(), 'converted_by': self.id})
        except (EngineError, OSError, UnicodeError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_conversion_failed'), exc)


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
