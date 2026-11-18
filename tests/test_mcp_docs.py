from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_readmes_explain_stdio_configuration():
    for name in ("README.md", "README.zh-CN.md", "README.ja.md", "README.ko.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for required in ('openmd-cli[mcp]', 'claude mcp add', '"mcpServers"', '"mcp"', 'openmd_job'):
            assert required in text, (name, required)


def test_skill_explains_mcp_limits_and_permissions():
    text = (ROOT / "src/openmd/SKILL.md").read_text(encoding="utf-8")
    for required in ("## MCP", "32000", "2 MiB", "include_content", "default off", "absolute"):
        assert required in text
