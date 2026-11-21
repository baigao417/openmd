from pathlib import Path
from subprocess import CompletedProcess

from openmd.installation import installation_manager, manager_command, next_action, parse_doctor_output
from openmd.cli import build_parser


def test_manager_detection_uses_environment_ownership_not_path(monkeypatch, tmp_path):
    uv_root = tmp_path / "uv-tools"
    pipx_root = tmp_path / "pipx-venvs"
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_root))
    monkeypatch.setenv("PIPX_HOME", str(pipx_root))
    assert installation_manager(uv_root / "openmd-cli" / "Scripts") == "uv"
    assert installation_manager(pipx_root / "venvs" / "openmd-cli" / "Scripts") == "pipx"
    assert installation_manager(tmp_path / "venv") == "pip"


def test_manager_commands_keep_upgrade_and_uninstall_scoped(monkeypatch):
    monkeypatch.setattr("openmd.installation.shutil.which", lambda name: f"/tools/{name}")
    assert manager_command("uv", "update") == ["/tools/uv", "tool", "upgrade", "openmd-cli"]
    assert manager_command("pipx", "uninstall") == ["/tools/pipx", "uninstall", "openmd-cli"]
    assert manager_command("uv", "uninstall") == ["/tools/uv", "tool", "uninstall", "openmd-cli"]


def test_uv_update_has_chinese_mirror_fallbacks(monkeypatch, tmp_path):
    from openmd import installation

    monkeypatch.setattr(installation, "data_directory", lambda: tmp_path)
    (tmp_path / "install.json").write_text(
        '{"schema":"openmd.install/v1","manager":"uv","manager_exe":"/tools/uv",'
        '"install_index":"https://pypi.tuna.tsinghua.edu.cn/simple"}', encoding="utf-8")
    monkeypatch.setattr("openmd.installation.Path.is_file", lambda _path: True)
    commands = installation.manager_commands("uv", "update")
    assert commands[0] == ["/tools/uv", "tool", "upgrade", "openmd-cli", "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"]
    assert commands[1][-1] == "https://mirrors.aliyun.com/pypi/simple"
    assert commands[2][-1] == "https://pypi.org/simple"


def test_next_action_contract_is_structured_and_does_not_expose_paths():
    result = next_action("update_failed", "network")
    assert result == {
        "error_code": "update_failed",
        "message": "network",
        "next_action": "Check the configured PyPI mirror and network, then rerun `openmd update`.",
    }


def test_parse_doctor_output_accepts_pretty_json():
    assert parse_doctor_output('''{
      "ok": true,
      "profile_diagnostics": {"ready": true}
    }\n''') == {"ok": True, "profile_diagnostics": {"ready": True}}


def test_lifecycle_commands_are_first_class_cli_commands():
    parser = build_parser()
    assert parser.parse_args(["update"]).lifecycle_action == "update"
    uninstall = parser.parse_args(["uninstall", "--yes", "--purge-data"])
    assert uninstall.lifecycle_action == "uninstall"
    assert uninstall.yes and uninstall.purge_data


def test_uninstall_removes_only_managed_runtime_by_default(monkeypatch, tmp_path, capsys):
    import argparse
    from openmd import installation

    runtime = tmp_path / "tools" / "opencli"
    runtime.mkdir(parents=True)
    (runtime / "managed.txt").write_text("managed")
    (tmp_path / "runs").mkdir()
    monkeypatch.setattr(installation, "installation_manager", lambda: "pip")
    monkeypatch.setattr(installation, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(installation, "manager_command", lambda *_: ["pip", "uninstall", "openmd-cli"])
    monkeypatch.setattr("openmd.browser_setup.uninstall", lambda: 0)
    monkeypatch.setattr("openmd.shell_menu.uninstall", lambda: {"removed": True})
    monkeypatch.setattr(installation.subprocess, "run", lambda *a, **k: CompletedProcess(a[0], 0))
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    result = installation.cmd_lifecycle(argparse.Namespace(lifecycle_action="uninstall", yes=True, purge_data=False))

    assert result == 0
    assert not runtime.exists()
    assert (tmp_path / "runs").is_dir()
    assert "pip_shared_dependencies_retained" in capsys.readouterr().out
