"""The package index and GitHub Release are separate delivery surfaces."""
from pathlib import Path
import re


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
    assert '.github/release-notes.md' in job and 'VERSION' in job
    assert 'skip-existing' not in job


def test_release_write_permission_is_limited_to_github_job():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text(encoding='utf-8')
    prefix = workflow.split('\n  github-release:', 1)[0]
    assert 'contents: write' not in prefix
    assert 'contents: read' in prefix
    assert 'id-token: write' in prefix


def test_release_notes_only_cover_updates_downloads_and_platform_support():
    root = Path(__file__).resolve().parents[1]
    notes = (root / '.github/release-notes.md').read_text(encoding='utf-8').replace('VERSION', '0.1.1')
    assert len(notes.splitlines()) <= 15
    assert [line for line in notes.splitlines() if line.startswith('#')] == [
        '### 更新 / Update', '### 下载 / Download',
    ]
    assert 'pip install -U openmd-cli' in notes
    assert 'openmd browser-setup  # 仅 Windows' in notes
    assert notes.count('openmd_cli-0.1.1-py3-none-any.whl') == 3
    for system in ('macOS', 'Linux'):
        row = next(line for line in notes.splitlines() if line.startswith(f'| {system} |'))
        assert '暂不支持浏览器扩展，CLI 可用' in row
    assert 'openmd-extension-0.1.1.zip' in notes
    assert not (root / 'CHANGELOG.md').exists()
