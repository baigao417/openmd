"""Format-level local availability, without opening input files or launching tools."""
import os

from .registry import engine_chain


def install_command(format):
    if format in {'mobi', 'azw3', 'calibre'}:
        return ('winget install calibre.calibre' if os.name == 'nt' else
                'brew install --cask calibre' if os.sys.platform == 'darwin' else 'sudo apt-get install calibre')
    if format in {'doc', 'ppt', 'soffice'}:
        return ('winget install TheDocumentFoundation.LibreOffice' if os.name == 'nt' else
                'brew install --cask libreoffice' if os.sys.platform == 'darwin' else 'sudo apt-get install libreoffice')
    if format == 'pandoc':
        return 'python -m pip install "openmd-cli[pandoc]"'
    if format == 'markitdown':
        return 'python -m pip install "markitdown[pdf,docx,pptx,xlsx,outlook]"'
    if format == 'defuddle':
        return 'npm install --prefix ./openmd-tools defuddle'
    return 'python -m pip install "openmd-cli[document]"'


def document_formats():
    from openmd.core.detector import DOCUMENT_EXTENSIONS
    from .tools import ExecutableEngine, executable
    cache, result = {}, {}
    for extension in sorted(DOCUMENT_EXTENSIONS):
        format = extension.lstrip('.')
        rows = []
        for engine in engine_chain(format):
            if engine.id not in cache:
                if isinstance(engine, ExecutableEngine):
                    ok = executable(engine.id) is not None
                    status = {'ok':ok, 'reason': 'Local executable detected; conversion not probed.' if ok else engine.install_hint, 'version': None}
                else:
                    status = engine.available()
                cache[engine.id] = status
            status = cache[engine.id]
            rows.append({'id': engine.id, 'available':status['ok'], 'reason':status['reason'],
                         'version':status['version'], 'install_command': install_command(engine.id)})
        result[format] = {'available': any(row['available'] for row in rows), 'engines':rows,
            'missing': [row['reason'] for row in rows if not row['available']],
            'install_command': install_command(format),
            'scope': 'Partial text/basic tables without LibreOffice; layout and active slide order not certified.' if format in {'doc','ppt'} else 'Calibre required; DRM rejected.' if format in {'mobi','azw3'} else 'Native document text; embedded image OCR is optional.'}
    return result
