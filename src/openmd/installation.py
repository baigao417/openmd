"""Installation manager detection and reversible lifecycle actions."""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

from openmd.core.preferences import data_directory
from openmd.runtime.interpreters import run_interruptibly


def _install_record() -> dict:
    try:
        value = json.loads((data_directory() / "install.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("schema") == "openmd.install/v1" else {}


def installation_manager(prefix: Path | None = None) -> str:
    """Identify the manager that owns this interpreter, never by PATH alone."""
    prefix = (prefix or Path(sys.prefix)).resolve()
    record = _install_record()
    recorded_manager = record.get("manager")
    recorded_tool_dir = record.get("tool_dir")
    if recorded_manager in {"uv", "pipx"} and isinstance(recorded_tool_dir, str):
        try:
            prefix.relative_to(Path(recorded_tool_dir).expanduser().resolve() / "openmd-cli")
            return recorded_manager
        except ValueError:
            pass
    uv_default = (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "uv" / "tools") if os.name == "nt" else Path.home() / ".local" / "share" / "uv" / "tools"
    pipx_home_default = (Path(os.environ.get("USERPROFILE", Path.home())) / ".local" / "pipx") if os.name == "nt" else Path.home() / ".local" / "pipx"
    pipx_dir = Path(os.environ.get("PIPX_HOME", pipx_home_default)).expanduser() / "venvs"
    uv_dir = Path(os.environ.get("UV_TOOL_DIR", uv_default)).expanduser()
    for name, root in (("uv", uv_dir), ("pipx", pipx_dir)):
        try:
            prefix.relative_to(root.resolve() / "openmd-cli")
            return name
        except ValueError:
            continue
    return "pip"


def manager_command(manager: str, action: str) -> list[str] | None:
    """Return the explicit manager command for an upgrade/uninstall action."""
    record = _install_record()
    recorded_exe = record.get("manager_exe") if record.get("manager") == manager else None
    exe = str(recorded_exe) if isinstance(recorded_exe, str) and Path(recorded_exe).is_file() else shutil.which(manager)
    if not exe:
        return None
    if manager == "uv":
        return [exe, "tool", "upgrade" if action == "update" else "uninstall", "openmd-cli"]
    if manager == "pipx":
        return [exe, "upgrade" if action == "update" else "uninstall", "openmd-cli"]
    if manager == "pip":
        if action == "update":
            spec = record.get("install_spec")
            if not isinstance(spec, str) or not spec.startswith("openmd-cli"):
                spec = "openmd-cli"
            return [sys.executable, "-m", "pip", "install", "--upgrade", spec]
        return [sys.executable, "-m", "pip", "uninstall", "openmd-cli"]
    return None


def _update_indexes() -> list[str]:
    record = _install_record()
    configured = record.get("install_index")
    return list(dict.fromkeys(x for x in (configured, "https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple", "https://pypi.org/simple") if isinstance(x, str) and x))


def manager_commands(manager: str, action: str) -> list[list[str]]:
    primary = manager_command(manager, action)
    if primary is None or action != "update":
        return [primary] if primary else []
    indexes = _update_indexes()
    if manager == "uv":
        return [[*primary, "--index-url", index] for index in indexes]
    return [primary for _index in indexes]  # pip and pipx receive the selected index through PIP_INDEX_URL.


def next_action(code: str, detail: str | None = None) -> dict[str, str]:
    """Common, machine-readable failure guidance used by CLI and receipts."""
    action = {
        "manager_missing": "Install or restore the package manager that owns this Openmd environment, then retry.",
        "update_failed": "Check the configured PyPI mirror and network, then rerun `openmd update`.",
        "uninstall_failed": "Run the displayed manager uninstall command after Openmd exits.",
        "doctor_failed": "Run `openmd doctor --json` and resolve its reported missing capability.",
        "pip_extras_not_recorded": "Pip does not store which extras were selected; rerun the matching `pip install --upgrade 'openmd-cli[extras]'` command to refresh those optional dependencies.",
    }.get(code, "Run `openmd doctor --json` and follow the reported next action.")
    value = {"error_code": code, "next_action": action}
    if detail:
        value["message"] = detail
    return value


def parse_doctor_output(stdout: str) -> dict:
    """Parse the full JSON document emitted by doctor (which may be pretty printed)."""
    value = json.loads(stdout)
    return value if isinstance(value, dict) else {}


def cmd_lifecycle(args) -> int:
    """Upgrade or uninstall the current install while preserving user data."""
    import json

    manager = installation_manager()
    action = args.lifecycle_action
    data = data_directory()
    runtime = data / "tools" / "opencli"
    if action == "uninstall":
        print(json.dumps({
            "command": "openmd uninstall",
            "manager": manager,
            "remove": ["Openmd package and manager-owned environment", "Openmd managed OpenCLI/Node runtime", "browser native host registration and copied extension", "Openmd shell-menu integration"],
            "keep": ["Openmd data/configuration/login state/run records/models", "package-manager shared dependencies and caches", "Chrome unpacked extension registration (remove manually in chrome://extensions)"],
            "managed_runtime_exists": runtime.exists(),
            "purge_data": bool(args.purge_data),
        }, ensure_ascii=False, sort_keys=True))
        if args.purge_data:
            if not sys.stdin.isatty() or input("输入 DELETE OPENMD DATA 确认删除数据目录：").strip() != "DELETE OPENMD DATA":
                print("purge_data_confirmation_required", file=sys.stderr)
                return 2
        elif not args.yes and (not sys.stdin.isatty() or input("继续卸载程序并保留数据？[y/N] ").strip().lower() != "y"):
            return 2
        # Integrations are removed through their existing owners.
        from openmd.browser_setup import uninstall as uninstall_browser
        from openmd.shell_menu import uninstall as uninstall_shell_menu
        try:
            browser_code = uninstall_browser()
            shell_result = uninstall_shell_menu()
            if browser_code != 0 or shell_result.get("removed") is False:
                print(json.dumps(next_action("uninstall_failed", "An integration could not be removed.")), file=sys.stderr)
                return 1
            if runtime.exists():
                shutil.rmtree(runtime)
        except OSError as exc:
            print(json.dumps(next_action("uninstall_failed", type(exc).__name__)), file=sys.stderr)
            return 1
    commands = manager_commands(manager, action)
    if not commands:
        print(json.dumps(next_action("manager_missing", manager)), file=sys.stderr)
        return 127
    if manager == "pip" and action == "uninstall":
        print("pip_shared_dependencies_retained: pip removes only openmd-cli; shared dependencies are untouched.")
    result = None
    try:
        for index, command in enumerate(commands):
            env = None
            if action == "update" and manager in {"pip", "pipx"}:
                env = {**os.environ, "PIP_INDEX_URL": _update_indexes()[index]}
            # Hidden, interruptible child (process contract); output is relayed
            # because a CREATE_NO_WINDOW child has no console of its own.
            result = run_interruptibly(command, check=False, capture_output=True, text=True, env=env)
            if action != "update":
                sys.stdout.write(result.stdout or "")
                sys.stderr.write(result.stderr or "")
            if result.returncode == 0:
                break
    except OSError as exc:
        print(json.dumps(next_action(f"{action}_failed", type(exc).__name__), ensure_ascii=False), file=sys.stderr)
        return 1
    if result is None or result.returncode:
        code = result.returncode if result is not None else 1
        print(json.dumps(next_action(f"{action}_failed", f"manager_exit_{code}"), ensure_ascii=False), file=sys.stderr)
        return code
    if action == "update":
        record = _install_record()
        doctor = run_interruptibly([sys.executable, "-m", "openmd", "doctor", "--profile", "core", "--json"], capture_output=True, text=True, check=False)
        try:
            doctor_payload = parse_doctor_output(doctor.stdout)
        except ValueError:
            doctor_payload = {}
        diagnostics = doctor_payload.get("profile_diagnostics", {})
        if not isinstance(diagnostics, dict) or diagnostics.get("error_code"):
            print(json.dumps(next_action("doctor_failed"), ensure_ascii=False), file=sys.stderr)
            return 1
        extension_target = data / "browser" / "extension"
        extension_state = "not_installed"
        if extension_target.is_dir():
            import json as _json
            target_literal = _json.dumps(str(extension_target))
            verify = "import json; from pathlib import Path; from openmd.browser_setup import EXTENSION_DIR; from openmd.browser_extension_sync import sync_extension; p,h=sync_extension(EXTENSION_DIR, Path(" + target_literal + ")); print(json.dumps({'path':str(p),'sha256':h}))"
            sync = run_interruptibly([sys.executable, "-c", verify], capture_output=True, text=True, check=False)
            if sync.returncode:
                print(json.dumps(next_action("update_failed", "extension_sync_failed"), ensure_ascii=False), file=sys.stderr)
                return 1
            extension_state = json.loads(sync.stdout.strip().splitlines()[-1])
        known_extras = manager in {"uv", "pipx"} or isinstance(record.get("install_spec"), str)
        notice = None
        if manager == "pip" and not known_extras:
            notice = next_action("pip_extras_not_recorded")
        print(json.dumps({"command": "openmd update", "ok": True, "manager": manager, "doctor_parsed": True, "doctor_ready": bool(doctor_payload.get("ok")), "extras_preserved": known_extras, "notice": notice, "extension_sync": extension_state}, sort_keys=True))
    elif args.purge_data:
        shutil.rmtree(data)
    return 0
