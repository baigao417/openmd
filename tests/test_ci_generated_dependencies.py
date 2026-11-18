"""Check generated declarations in a checkout populated with Node dependencies."""
from pathlib import Path


def test_ci_installs_integration_dependencies_before_generated_check():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    install = workflow.index("      - name: Install integration dependencies for generated checks\n")
    check = workflow.index("      - name: Check generated declarations\n")
    assert install < check
    step = workflow[install:check]
    assert "shell: bash" in step
    assert "integrations/opencli/*/" in step
    assert '"$directory/package.json"' in step
    assert '"$directory/package-lock.json"' in step
    assert 'npm ci --prefix "$directory"' in step
    assert 'npm install --prefix "$directory"' in step
    assert step.count("--ignore-scripts --no-audit --no-fund") == 2
    assert "python -m openmd.routing.generate --check" in workflow[check:]
