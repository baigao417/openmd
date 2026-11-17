"""The package index and GitHub Release are separate delivery surfaces."""
from pathlib import Path
import re
import subprocess
import sys

import pytest


def test_github_release_waits_for_pypi_and_attaches_built_packages():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text(encoding='utf-8')
    match = re.search(r'^  github-release:\n(?P<body>(?:[ \t].*\n|\n)*)', workflow, re.MULTILINE)
    assert match, 'PyPI publication must also create a GitHub Release'
    job = match['body']
    assert 'needs: [build, publish]' in job
    assert 'contents: write' in job
    assert 'actions/download-artifact@v4' in job
    assert 'name: dist' in job and 'path: dist/' in job
    assert 'GH_TOKEN: ${{ github.token }}' in job
    assert 'GH_REPO: ${{ github.repository }}' in job
    assert 'gh release create "$GITHUB_REF_NAME"' in job
    assert '--verify-tag' in job and '--notes-file release-notes.md' in job
    assert '--generate-notes' not in job
    assert 'dist/*.whl dist/*.tar.gz extension-dist/*.zip' in job
    assert 'python scripts/package_extension.py --output-dir extension-dist' in workflow
    assert 'path: extension-dist/' in workflow
    assert 'python scripts/generate_release_notes.py --version "${GITHUB_REF_NAME#v}" --output release-notes.md' in job
    assert 'skip-existing' not in job


def test_release_write_permission_is_limited_to_github_job():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text(encoding='utf-8')
    prefix = workflow.split('\n  github-release:', 1)[0]
    assert 'contents: write' not in prefix
    assert 'contents: read' in prefix
    assert 'id-token: write' in prefix


@pytest.mark.parametrize('version', ['0.1.0', '0.1.1'])
def test_release_notes_include_only_selected_changelog_then_update_and_download(version, tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'notes.md'
    subprocess.run([sys.executable, str(root / 'scripts/generate_release_notes.py'),
                    '--version', version, '--output', str(output)], cwd=tmp_path, check=True)
    notes = output.read_text(encoding='utf-8')
    assert len(notes.splitlines()) <= 15
    assert [line for line in notes.splitlines() if line.startswith('#')] == [
        '### 新增 / Added', '### 修复 / Fixed',
        '### 更新 / Update', '### 下载 / Download',
    ]
    changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    section = re.search(rf'^## \[{re.escape(version)}\]\n(.*?)(?=^## |\Z)', changelog, re.M | re.S)[1].strip()
    assert notes.startswith(section + '\n')
    other = '0.1.0' if version == '0.1.1' else '0.1.1'
    assert other not in notes
    assert 'pip install -U openmd-cli' in notes
    assert 'openmd browser-setup  # 仅 Windows' in notes
    assert notes.count(f'openmd_cli-{version}-py3-none-any.whl') == 3
    for system in ('macOS', 'Linux'):
        row = next(line for line in notes.splitlines() if line.startswith(f'| {system} |'))
        assert '暂不支持浏览器扩展，CLI 可用' in row
    assert f'openmd-extension-{version}.zip' in notes


def test_release_notes_reject_missing_version_without_writing(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'notes.md'
    result = subprocess.run([sys.executable, str(root / 'scripts/generate_release_notes.py'),
                             '--version', '9.9.9', '--output', str(output)], capture_output=True)
    assert result.returncode != 0
    assert b'CHANGELOG' in result.stderr and b'9.9.9' in result.stderr
    assert not output.exists()
