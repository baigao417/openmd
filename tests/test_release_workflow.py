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
    assert '--verify-tag' in job and '--generate-notes' in job
    assert 'dist/*.whl dist/*.tar.gz' in job
    assert 'skip-existing' not in job


def test_release_write_permission_is_limited_to_github_job():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text(encoding='utf-8')
    prefix = workflow.split('\n  github-release:', 1)[0]
    assert 'contents: write' not in prefix
    assert 'contents: read' in prefix
    assert 'id-token: write' in prefix
