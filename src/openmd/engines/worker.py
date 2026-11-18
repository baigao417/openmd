"""Own and bound document parser subprocesses, including ZIP entry parsers."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

from openmd.runtime.tempdirs import owned_temporary_directory

from .base import ArchiveBudget, EngineError, EngineLimits


def convert_in_worker(path: Path, limits: EngineLimits, *, mode: str = 'document') -> dict:
    from .tools import run_command
    with owned_temporary_directory(prefix='openmd-document-worker-') as owned_root:
        root = Path(owned_root)
        request = root / 'request.json'
        output = root / 'result.json'
        request.write_text(json.dumps({'path': str(path.resolve()), 'limits': asdict(limits), 'mode': mode}), encoding='utf-8')
        bootstrap = 'import sys; sys.path[:0] = ' + repr(sys.path) + '; from openmd.engines.worker import main; main()'
        run_command([sys.executable, '-I', '-X', 'utf8', '-c', bootstrap, str(request), str(output)], root, limits)
        if not output.is_file() or output.stat().st_size > limits.max_output_bytes * 2:
            raise EngineError('document_output_limit')
        value = json.loads(output.read_text(encoding='utf-8'))
        if 'worker_error' in value:
            raise EngineError(value['worker_error'])
        return value


def main():
    request, output = map(Path, sys.argv[1:3])
    value = json.loads(request.read_text(encoding='utf-8'))
    settings = value['limits']
    settings['budget'] = ArchiveBudget(**settings['budget'])
    limits = EngineLimits(**settings)
    path = Path(value['path'])
    try:
        if value['mode'] == 'legacy_pipeline':
            from .sniff import validate_document
            validate_document(path, limits)
            from openmd.adapters.registry import create_default_registry
            from openmd.backends.registry import create_runtime_backend_provider_registry, BackendRuntimeOptions
            from openmd.backends.executor import BackendExecutor
            adapters = create_default_registry()
            adapter = adapters.adapter_for(str(path))
            item = adapter.resolve_items(str(path))[0]
            providers = create_runtime_backend_provider_registry(item.platform, BackendRuntimeOptions())
            result = asdict(BackendExecutor(provider_registry=providers).extract(item, adapters.backend_chain(item.platform)))
        elif value['mode'] == 'legacy_markitdown':
            from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
            result = asdict(MarkItDownPDFParser()._parse_legacy(str(path), title=path.stem))
        elif value['mode'] == 'qq_export':
            from openmd.adapters.qq_group.adapter import QQGroupAdapter
            adapter = QQGroupAdapter()
            result = asdict(adapter.extract_text(adapter.resolve_items(str(path))[0]))
        elif value['mode'] == 'markitdown_text':
            from .markitdown import convert_text_local
            result = {'text': convert_text_local(path)}
        else:
            from .registry import convert_document
            result = asdict(convert_document(path, limits))
        if len(json.dumps(result).encode('utf-8')) > limits.max_output_bytes * 2:
            raise EngineError('document_output_limit')
    except Exception as exc:
        result = {'worker_error': getattr(exc, 'code', 'document_extraction_failed')}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
