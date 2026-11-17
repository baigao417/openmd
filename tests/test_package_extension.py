"""Release ZIPs preserve the shipped extension, including its fixed identity."""
import json
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def test_extension_zip_preserves_manifest_and_all_referenced_files(tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / 'scripts/package_extension.py'),
         '--version', '0.1.1', '--output-dir', str(tmp_path)],
        check=True, cwd=tmp_path,
    )
    source = ROOT / 'src/openmd/browser_extension'
    with ZipFile(tmp_path / 'openmd-extension-0.1.1.zip') as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        original = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['version'] == original['version']
        assert manifest['key'] == original['key']
        assert bundle.read('manifest.json') == (source / 'manifest.json').read_bytes()
        files = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
        assert set(bundle.namelist()) == files
        references = [manifest['background']['service_worker'], manifest['action']['default_popup'],
                      manifest['options_page'], *manifest['icons'].values(),
                      *manifest['action']['default_icon'].values()]
        for name in bundle.namelist():
            if name.endswith(('.html', '.js', '.css')):
                text = bundle.read(name).decode('utf-8')
                refs = re.findall(r'''(?:src=|href=|from\s+|import\s*)["']([^"']+)["']''', text)
                references.extend((Path(name).parent / ref).as_posix() for ref in refs
                                  if not ref.startswith(('http:', 'https:', '#', 'data:')))
        assert all(name in bundle.namelist() for name in references)
