"""Safe argv execution and local launcher resolution for tool cards."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openmd.core.local_config import load_local_config
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import owned_popen, wait_interruptibly, terminate_quietly as _kill_tree


class ToolError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(detail or code)


@dataclass(frozen=True)
class Launcher:
    argv: tuple[str, ...]
    origin: str
    entry_path: Path


@dataclass(frozen=True)
class ToolRun:
    exit_code: int
    seconds: float
    stdout: str
    stderr: str
    files: dict[str, dict[str, Any]]


def _executable(value: str) -> str:
    resolved = shutil.which(value) or (str(Path(value).expanduser().resolve()) if Path(value).expanduser().is_file() else None)
    if not resolved:
        raise ToolError("tool_not_installed", "Executable not found; run openmd doctor --profile tools.")
    if Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        raise ToolError("tool_launcher_unsafe", "Batch launchers cannot safely carry URLs; configure a native executable or Node entry.")
    return resolved


def resolve_launcher(card: dict[str, Any]) -> Launcher:
    tool_id = card["id"]
    config = load_local_config().get("tools", {}).get(tool_id, {})
    if not isinstance(config, dict) or set(config) - {"python", "paths", "root", "node", "script_root"}:
        raise ToolError("tool_location_invalid")
    config = dict(config)
    prefix = "OPENMD_TOOL_" + re.sub(r"[^A-Z0-9]", "_", tool_id.upper())
    origin = "local_config" if config else "which"
    for key in ("python", "root", "node", "script_root"):
        if os.getenv(prefix + "_" + key.upper()):
            config[key] = os.environ[prefix + "_" + key.upper()]
            origin = "env"
    launcher = card.get("launcher", {})
    if card["kind"] == "command" and launcher.get("node_entry"):
        root = config.get("root")
        if root:
            entry = Path(root).expanduser() / launcher["node_entry"]
        else:
            which = next((shutil.which(x) for x in launcher.get("which", []) if shutil.which(x)), None)
            if not which:
                raise ToolError("tool_not_installed")
            entry = Path(which).parent / "node_modules" / launcher["node_entry"]
        if not entry.is_file():
            raise ToolError("tool_not_installed", "Node entry not found; configure tool root and run openmd doctor --profile tools.")
        node = _executable(config.get("node", "node"))
        if launcher.get("node_api"):
            import json

            # Library-only options are declarative; invoke the unchanged CLI
            # after configuring its shared exported parser. All values are argv.
            api = launcher["node_api"]
            api_path = ((Path(root) if root else entry.parents[2]) / api["module"]).resolve()
            if not api_path.is_file():
                raise ToolError("tool_not_installed")
            code = "const [entry,apiPath,name,options,...args]=process.argv.slice(1);const api=require(apiPath),original=api[name],overrides=JSON.parse(options);api[name]=function(...a){a[a.length-1]={...a[a.length-1],...overrides};return original.apply(this,a)};require(entry).createProgram().parse([process.execPath,entry,...args]);"
            return Launcher((node, "-e", code, str(entry.resolve()), str(api_path), api["export"], json.dumps(api["options"])), origin, entry.resolve())
        return Launcher((node, str(entry.resolve())), origin, entry.resolve())
    if card["kind"] in {"python_entry", "python_script"}:
        if not config:
            raise ToolError("tool_not_installed", "Configure the isolated Python interpreter and dependency paths; direct console scripts cannot enforce this card's TLS and credential boundary.")
        python = _executable(config.get("python", sys.executable))
        paths = config.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) and Path(p).is_dir() for p in paths):
            raise ToolError("tool_not_installed", "Isolated Python dependency directory is missing.")
        args = [python, "-I", "-X", "utf8", str(Path(__file__).with_name("entry_shim.py").resolve())]
        for path in paths:
            args.extend(("--path", str(Path(path).resolve())))
        if card["kind"] == "python_script":
            root = config.get("script_root")
            if not isinstance(root, str):
                raise ToolError("tool_not_installed")
            entry = Path(root).expanduser() / launcher["script"]
            if not entry.is_file():
                raise ToolError("tool_not_installed")
            args.extend(("--script", str(entry.resolve())))
        else:
            module = launcher["entry"].split(":", 1)[0]
            relative = Path(*module.split("."))
            entries = [Path(p) / relative for p in paths]
            entry = next((q.with_suffix(".py") for q in entries if q.with_suffix(".py").is_file()), None)
            if entry is None:
                entry = next((q / "__init__.py" for q in entries if (q / "__init__.py").is_file()), None)
            if entry is None and not paths:
                spec = importlib.util.find_spec(module.split(".")[0])
                entry = Path(spec.origin) if spec and spec.origin else None
            if entry is None:
                raise ToolError("tool_not_installed", "Python entry not installed; run openmd doctor --profile tools.")
            args.extend(("--entry", launcher["entry"]))
        return Launcher(tuple([*args, "--"]), origin, entry.resolve())
    raise ToolError("tool_launcher_invalid")


def validate_url(url: str, capability: dict[str, Any]) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in url):
        raise ToolError("tool_input_invalid", "Tool requires an HTTP(S) source URL without embedded credentials.")
    if capability.get("url_hosts") and parsed.hostname.lower() not in capability["url_hosts"]:
        raise ToolError("tool_input_unsupported")
    if capability.get("url_path_pattern") and not re.fullmatch(capability["url_path_pattern"], parsed.path):
        raise ToolError("tool_input_unsupported", "This card supports individual public items only.")


def build_argv(launcher: Launcher, capability: dict[str, Any], *, url: str, item_id: str | None, workdir: Path) -> list[str]:
    validate_url(url, capability)
    values = {"url": url, "id": item_id or url.rstrip("/").split("/")[-1], "workdir": str(workdir.resolve())}
    arguments: list[str] = []
    for template in capability["argv"]:
        if any(name not in values for name in re.findall(r"\{([^{}]+)\}", template)):
            raise ToolError("tool_placeholder_invalid")
        argument = template.format_map(values)
        arguments.append(argument)
    return [*launcher.argv, *arguments]


def isolated_environment(workdir: Path) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    home = workdir / ".home"
    home.mkdir(exist_ok=True)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        env[key] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_tool(card: dict[str, Any], capability: dict[str, Any], item, workdir: Path) -> ToolRun:
    launcher = resolve_launcher(card)
    argv = build_argv(launcher, capability, url=item.url, item_id=item.id, workdir=workdir)
    stdout_path, stderr_path = workdir / ".stdout", workdir / ".stderr"
    started = time.monotonic()
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = owned_popen(argv, cwd=workdir, env=isolated_environment(workdir), stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False, start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            try:
                wait_interruptibly(process, capability["timeout_s"])
            except subprocess.TimeoutExpired as exc:
                _kill_tree(process)
                raise ToolError("tool_timeout", "Command timed out; its owned process tree was terminated.") from exc
            except BaseException:
                _kill_tree(process)
                raise
        if stdout_path.stat().st_size > 16 * 1024 * 1024 or stderr_path.stat().st_size > 16 * 1024 * 1024:
            raise ToolError("tool_output_too_large")
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        artifacts: dict[str, dict[str, Any]] = {}
        for path in workdir.rglob("*"):
            if path.is_file() and path.name not in {".stdout", ".stderr", ".openmd-owner.json"} and ".home" not in path.relative_to(workdir).parts:
                if not path.resolve().is_relative_to(workdir.resolve()):
                    raise ToolError("tool_artifact_path_invalid")
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                artifacts[path.relative_to(workdir).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
        return ToolRun(process.returncode, round(time.monotonic() - started, 3), stdout, stderr, artifacts)
    except OSError as exc:
        raise ToolError("tool_launch_failed", sanitize_error_summary(exc) or "Tool launch failed.") from exc
