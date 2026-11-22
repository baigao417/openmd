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
    monkeypatch.setattr(installation, "run_interruptibly", lambda *a, **k: CompletedProcess(a[0], 0, "", ""))
    monkeypatch.setattr(installation.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    result = installation.cmd_lifecycle(argparse.Namespace(lifecycle_action="uninstall", yes=True, purge_data=False))

    assert result == 0
    assert not runtime.exists()
    assert (tmp_path / "runs").is_dir()
    assert "pip_shared_dependencies_retained" in capsys.readouterr().out


def _uv_record(tmp_path):
    root = tmp_path / "Openmd"
    return {"schema": "openmd.install/v1", "manager": "uv", "manager_exe": str(root / "manager" / "uv.exe"),
            "tool_dir": str(root / "uv" / "tools"), "tool_bin": str(root / "bin"), "python_dir": str(root / "uv" / "python"),
            "cache_dir": str(root / "uv" / "cache"), "install_root": str(root), "path_entry_added": True}


def test_uv_lifecycle_commands_use_the_recorded_tool_directories(monkeypatch, tmp_path):
    """A new terminal has no UV_* variables; update/uninstall must not fall back to uv's default tool dir."""
    from openmd import installation

    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    env = installation.uv_environment(_uv_record(tmp_path))
    assert env["UV_TOOL_DIR"] == str(tmp_path / "Openmd" / "uv" / "tools")
    assert env["UV_TOOL_BIN_DIR"] == str(tmp_path / "Openmd" / "bin")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "Openmd" / "uv" / "python")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "Openmd" / "uv" / "cache")
    assert installation.uv_environment({"manager": "pip"}) is None


def test_windows_uv_uninstall_is_finished_after_openmd_exits(monkeypatch, tmp_path, capsys):
    import argparse
    import json
    from openmd import installation

    data = tmp_path / "data"
    data.mkdir()
    record = _uv_record(tmp_path)
    (data / "install.json").write_text(json.dumps(record), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(installation.os, "name", "nt")
    monkeypatch.setattr(installation, "installation_manager", lambda: "uv")
    monkeypatch.setattr(installation, "data_directory", lambda: data)
    monkeypatch.setattr(installation, "manager_command", lambda *_: [record["manager_exe"], "tool", "uninstall", "openmd-cli"])
    monkeypatch.setattr("openmd.browser_setup.uninstall", lambda: 0)
    monkeypatch.setattr("openmd.shell_menu.uninstall", lambda: {"removed": True})
    monkeypatch.setattr("openmd.runtime.interpreters.spawn_detached", lambda command, **kw: spawned.append(command) or 4242)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(installation, "run_interruptibly", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run in-process")))

    result = installation.cmd_lifecycle(argparse.Namespace(lifecycle_action="uninstall", yes=True, purge_data=False))

    assert result == 0
    assert len(spawned) == 1 and spawned[0][-2] == "-File"
    script = open(spawned[0][-1], encoding="utf-8-sig").read()
    assert "'tool' 'uninstall' 'openmd-cli'" in script
    assert f"$env:UV_TOOL_DIR = '{record['tool_dir']}'" in script
    assert record["tool_bin"] in script and "path_entry_removed" in script
    assert str(data / "install.json") in script
    assert str(data) not in script.replace(str(data / "install.json"), "").replace(str(data / "runs"), "")  # never removes data
    assert data.is_dir()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["scheduled"] is True


def test_version_flag_prints_the_package_version(capsys):
    from openmd import __version__
    from openmd.cli import main

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"openmd {__version__}"
