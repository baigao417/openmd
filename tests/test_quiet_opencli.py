"""Round 10 managed OpenCLI and owned browser regressions (offline)."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from openmd.core import local_config
from openmd.runtime import managed_tools, services


def test_managed_always_beats_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(managed_tools, "_managed_ready", lambda: True)
    monkeypatch.setattr(managed_tools, "node_dir", lambda: None)
    monkeypatch.setattr(managed_tools, "_prepare_bundled_node_host_package", lambda: None)
    monkeypatch.setattr(managed_tools.shutil, "which", lambda *a, **k: str(tmp_path / "fake/opencli"))
    assert managed_tools.activate() == "managed"
    assert os.environ["PATH"].split(os.pathsep)[0] == str(managed_tools.managed_bin())
    assert os.environ["OPENCLI_CLOSE_OWNED_TAB"] == "1"
    assert os.environ["OPENCLI_CONFIG_DIR"] == str(managed_tools.config_directory())


@pytest.mark.parametrize("value", ["", 42, None, [], {}])
def test_config_dir_schema_rejects_bad_values(tmp_path, monkeypatch, value):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"opencli": {"root": str(tmp_path), "config_dir": value}}}))
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    with pytest.raises(ValueError, match="managed_opencli_location_invalid"):
        local_config.load_local_config()


def test_config_dir_supported(monkeypatch, tmp_path):
    monkeypatch.setattr(managed_tools, "load_local_config", lambda: {"tools": {"opencli": {"root": str(tmp_path), "config_dir": str(tmp_path / "config")}}})
    assert managed_tools.managed_root() == tmp_path
    assert managed_tools.config_directory() == tmp_path / "config"


@pytest.mark.parametrize("strategy", [[], {}])
def test_invalid_browser_strategy_is_a_configuration_error(tmp_path, monkeypatch, strategy):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "browser": {"window_strategy": strategy}}))
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    with pytest.raises(ValueError, match="local_config_browser_invalid"):
        local_config.load_local_config()


def test_migrate_dry_run_never_opens_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "credential.json").write_bytes(b"not read")
    monkeypatch.setattr(Path, "open", Mock(side_effect=AssertionError("dry-run read file")))
    receipt = managed_tools.migrate_config(source=source, destination=tmp_path / "to")
    assert receipt["files"] == [{"name": "credential.json", "size": 8}]
    assert not (tmp_path / "to").exists()


def test_migrate_preserves_source_and_existing_destination(tmp_path):
    source, target = tmp_path / "from", tmp_path / "to"
    source.mkdir()
    target.mkdir()
    (source / "one").write_text("first")
    (target / "one").write_text("keep")
    (source / "two").write_text("second")
    receipt = managed_tools.migrate_config(source=source, destination=target, apply=True)
    assert (source / "two").read_text() == (target / "two").read_text()
    assert (source / "two").stat().st_mtime_ns == (target / "two").stat().st_mtime_ns
    assert (target / "one").read_text() == "keep"
    assert receipt["conflicts"] == ["one"]


def test_credentials_prefer_config_then_legacy(tmp_path, monkeypatch):
    from openmd.backends import xiaoyuzhou
    config, legacy = tmp_path / "config", tmp_path / "legacy.json"
    config.mkdir()
    legacy.touch()
    monkeypatch.setattr(managed_tools, "config_directory", lambda: config)
    monkeypatch.setattr(xiaoyuzhou, "XIAOYUZHOU_CREDENTIALS_PATH", legacy)
    assert xiaoyuzhou.credential_file() == legacy
    (config / "xiaoyuzhou.json").touch()
    assert xiaoyuzhou.credential_file() == config / "xiaoyuzhou.json"


def test_one_shot_sessions_are_ephemeral():
    from openmd.backends.x import _browser_options
    from openmd.backends.xiaoe import build_opencli_xiaoe_play_url_args, build_opencli_xiaoe_content_args, build_opencli_xiaoe_catalog_args
    for args in [_browser_options(), *[f("https://example.org") for f in (build_opencli_xiaoe_play_url_args, build_opencli_xiaoe_content_args, build_opencli_xiaoe_catalog_args)]]:
        assert "persistent" not in args


def test_generated_owned_lifecycle():
    bridge = (Path(__file__).parents[1] / "src/openmd/browser_extension/vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    assert "if (adapterPersistent) return IDLE_TIMEOUT_NONE" not in bridge
    assert "openmdMuteOwnedTab" in bridge
    assert "openmd-cleanup-orphans" in bridge
    assert 'state:"minimized"' in bridge
    assert "left: -32000" not in bridge
    assert "openmdOptions.windowStrategy" in bridge
    release = bridge.split('async function releaseLease(')[1].split('async function reconcileTargetLeaseRegistry')[0]
    assert "parkIdleInteractiveTab" not in release
    assert "chrome.tabs.ungroup" in release and "chrome.tabs.remove" in release


def test_passthrough_blocks_write(monkeypatch, capsys):
    from openmd.cli_commands import cmd_opencli
    from argparse import Namespace
    monkeypatch.setattr(managed_tools, "ensure_opencli", lambda: "managed")
    monkeypatch.setattr(managed_tools, "command_access", lambda args: "write")
    from openmd.runtime import opencli_adapters
    monkeypatch.setattr(opencli_adapters, "prepare_bundled_plugins", lambda: [])
    run = Mock()
    monkeypatch.setattr(managed_tools, "passthrough", run)
    assert cmd_opencli(Namespace(opencli_args=["twitter", "post", "hello"])) == 2
    run.assert_not_called()


def test_passthrough_receives_registered_access_level(monkeypatch):
    from openmd.cli_commands import cmd_opencli
    from argparse import Namespace

    monkeypatch.setattr(managed_tools, "ensure_opencli", lambda: "managed")
    monkeypatch.setattr(managed_tools, "command_access", lambda args: "read")
    from openmd.runtime import opencli_adapters
    monkeypatch.setattr(opencli_adapters, "prepare_bundled_plugins", lambda: [])
    run = Mock(return_value=0)
    monkeypatch.setattr(managed_tools, "passthrough", run)
    assert cmd_opencli(Namespace(opencli_args=["bilibili", "subtitle", "BV123"])) == 0
    run.assert_called_once_with(["bilibili", "subtitle", "BV123"], access="read")


@pytest.mark.parametrize("payload", [None, {"daemonVersion": "2.0.3"}, {"daemonVersion": "0.9.0"}])
def test_daemon_identity_states(monkeypatch, payload):
    monkeypatch.setattr(services, "daemon_status", lambda: payload)
    if payload and payload["daemonVersion"] != managed_tools.OPENCLI_VERSION:
        with pytest.raises(OSError, match="opencli_daemon_foreign"):
            services.check_daemon_version()
    else:
        services.check_daemon_version()


def test_environment_aliases_only_child_home(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_tools, "config_directory", lambda: tmp_path / "config")
    before = Path.home()
    env = managed_tools.command_environment({"PATH": "fake", "OPENMD_BROWSER_SOUND": "1"})
    assert Path.home() == before
    assert Path(env["USERPROFILE"], ".opencli").resolve() == tmp_path / "config"
    assert env["OPENCLI_CONFIG_DIR"] == str(tmp_path / "config")
    assert env["OPENCLI_CLOSE_OWNED_TAB"] == "1"
    assert env["OPENMD_BROWSER_SOUND"] == "1"


def test_passthrough_original_exit_and_streams(monkeypatch):
    monkeypatch.setattr(managed_tools, "managed_command", lambda a: ["pinned", *a])
    monkeypatch.setattr(managed_tools, "command_environment", lambda: {"OPENCLI_CONFIG_DIR": "managed"})
    from openmd.runtime import opencli_adapters
    monkeypatch.setattr(opencli_adapters, "prepare_bundled_plugins", lambda: [])
    run = Mock(return_value=Mock(returncode=23))
    monkeypatch.setattr(managed_tools, "run_interruptibly", run)
    assert managed_tools.passthrough(["hackernews", "top"]) == 23
    assert run.call_args.args[0] == ["pinned", "hackernews", "top"]
    assert "capture_output" not in run.call_args.kwargs
    assert run.call_args.kwargs["env"]["OPENCLI_CONFIG_DIR"] == "managed"


def test_bilibili_subtitle_passthrough_uses_loader_environment(monkeypatch):
    monkeypatch.setattr(managed_tools, "managed_command", lambda args: ["node", *args])
    monkeypatch.setattr(managed_tools, "command_environment", lambda base=None: dict(base or {}))
    monkeypatch.setattr("openmd.runtime.opencli_adapters.prepare_bundled_plugins", lambda: [])
    monkeypatch.setattr("openmd.runtime.ai_chat_loading.detail_loading_environment", lambda site, operation: {
        "OPENMD_TEST_LOADER": f"{site}:{operation}"
    })
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(managed_tools, "run_interruptibly", run)

    assert managed_tools.passthrough(["bilibili", "subtitle", "BV123"], access="read") == 0
    assert run.call_args.kwargs["env"]["OPENMD_TEST_LOADER"] == "bilibili:subtitle"


def test_passthrough_real_inherited_stdout(tmp_path):
    import sys
    import subprocess
    from openmd.runtime.interpreters import run_interruptibly
    script = tmp_path / "forward.py"
    body = "import sys;sys.stdout.buffer.write(b'raw-output\\n');sys.stderr.buffer.write(b'raw-error\\n');sys.exit(17)"
    script.write_text("\n".join([
        "import sys,os", "from openmd.runtime import managed_tools,opencli_adapters",
        "opencli_adapters.prepare_bundled_plugins=lambda: []",
        "managed_tools.command_environment=lambda: dict(os.environ)",
        f"managed_tools.managed_command=lambda args: [sys.executable,'-c',{body!r}]",
        "sys.exit(managed_tools.passthrough(['fixture','read']))",
    ]), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = run_interruptibly([sys.executable, str(script)], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15)
    assert result.returncode == 17
    assert result.stdout == b"raw-output\n"
    assert result.stderr == b"raw-error\n"


def test_all_direct_windows_process_launches_are_hidden():
    import ast
    root = Path(__file__).parents[1] / "src/openmd"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(function):
                if not isinstance(call, ast.Call) or ast.unparse(call.func) not in {"subprocess.Popen", "subprocess.run", "subprocess.check_output"}:
                    continue
                body = ast.get_source_segment(source, function)
                assert "CREATE_NO_WINDOW" in body or "process_options()" in body, (path, function.name)


def test_owned_release_does_not_close_user_tab(tmp_path):
    import shutil
    import subprocess
    from openmd.runtime.interpreters import run_interruptibly
    node_dir = managed_tools.node_dir()
    if node_dir is None:
        pytest.skip("Node not installed")
    node = str(node_dir / ("node.exe" if os.name == "nt" else "node"))
    source = (Path(__file__).parents[1] / "src/openmd/browser_extension/vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    helper = source[source.index("const openmdOptions ="):source.index("async function handleCommand(cmd)")]
    release = "async function releaseLease(" + source.split("async function releaseLease(")[1].split("async function reconcileTargetLeaseRegistry")[0]
    script = r'''
const assert = require('node:assert/strict');
const removed=[],muted=[],groups=[];
const currentContextId='openmd-a1b2c3d4';const activeCommandCounts=new Map();
const automationSessions=new Map([
 ['owned',{owned:true,contextId:'openmd-a1b2c3d4',openmdOwnerPid:123,preferredTabId:10,windowId:1,session:'owned'}],
 ['user',{owned:false,contextId:'openmd-a1b2c3d4',openmdOwnerPid:123,preferredTabId:20,windowId:1,session:'user'}],
 ['active',{owned:true,contextId:'openmd-a1b2c3d4',openmdOwnerPid:456,idleDeadlineAt:Date.now()+120000,preferredTabId:30,windowId:1,session:'active'}],
 ['foreign',{owned:true,contextId:'other',openmdOwnerPid:123,preferredTabId:40,windowId:1,session:'foreign'}]
]);
const sessionOverrides=new Map();const IDLE_TIMEOUT_NONE=0;
const scheduleIdleAlarm=()=>{},persistRuntimeState=async()=>{},safeDetach=async()=>{},evictTab=()=>{};
const chrome={windows:{getAll:async()=>[{id:1}]},tabs:{onRemoved:{addListener:()=>{}},get:async id=>({id}),ungroup:async id=>groups.push(id),remove:async id=>removed.push(id),update:async(id,opts)=>muted.push([id,opts])}};
'''+helper+release+r'''
(async()=>{
 const reply=await openmdControl({id:'fixture',contextId:'openmd-a1b2c3d4',action:'openmd-cleanup-orphans',deadPids:[123]});
 assert.equal(reply.data.tabsClosed,1);assert.equal(reply.data.failures,0);
 assert(automationSessions.has('user'));assert(automationSessions.has('active'));assert(automationSessions.has('foreign'));
 await releaseLease('user');assert.deepEqual(removed,[10]);assert.deepEqual(groups,[10]);assert.equal(muted.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    harness = tmp_path / "owned.cjs"
    harness.write_text(script, encoding="utf-8")
    result = run_interruptibly([node, str(harness)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancel"])
def test_tingwu_multistep_always_closes_its_unique_session(tmp_path, monkeypatch, outcome):
    import subprocess
    from openmd.runtime import opencli_adapters

    host = tmp_path / "host"
    host.mkdir()
    (host / "package.json").write_text(json.dumps({"version": managed_tools.OPENCLI_VERSION}))
    monkeypatch.delenv("OPENMD_XIAOE_HOST", raising=False)
    monkeypatch.setattr(opencli_adapters, "managed_package_root", lambda: host)
    monkeypatch.setattr(opencli_adapters, "validate_dispatcher", lambda host: None)
    monkeypatch.setattr(opencli_adapters, "node_dir", lambda: tmp_path)
    monkeypatch.setattr(opencli_adapters, "command_environment", lambda: {})
    monkeypatch.setattr(opencli_adapters, "managed_command", lambda args: ["managed", *args])
    monkeypatch.setattr(services, "ensure_opencli_daemon", Mock())
    runner = Mock(return_value=Mock(returncode=0, stdout='{"ok":true}'))
    if outcome == "error":
        runner.side_effect = OSError("fixture")
    elif outcome == "timeout":
        runner.side_effect = subprocess.TimeoutExpired("fixture", 1)
    elif outcome == "cancel":
        runner.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(opencli_adapters, "run_interruptibly", runner)
    close = Mock()
    monkeypatch.setattr(opencli_adapters, "close_tingwu_session", close)
    if outcome == "cancel":
        with pytest.raises(KeyboardInterrupt):
            opencli_adapters.run_tingwu(tmp_path / "audio.mp3", duration=1, timeout=1)
    else:
        code, _, _ = opencli_adapters.run_tingwu(tmp_path / "audio.mp3", duration=1, timeout=1)
        assert code == (0 if outcome == "success" else 1)
    close.assert_called_once()
    session = close.call_args.args[0]
    assert session.startswith("site:openmd-tingwu-")
    assert session == "site:" + runner.call_args.kwargs["env"]["OPENMD_TINGWU_SITE"]


def test_all_owned_creation_paths_use_blank_then_mute():
    source = (Path(__file__).parents[1] / "src/openmd/browser_extension/vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    # The only raw creation calls are the two helpers. All lease sites use them.
    assert source.count("chrome.tabs.create(") == 1
    assert source.count("chrome.windows.create(") == 1
    helper = source.split("async function openmdCreateOwnedTab(")[1].split("async function openmdControl(")[0]
    assert helper.count('url: "about:blank"') == 1
    assert helper.count("await openmdMuteOwnedTab(") == 2


@pytest.mark.parametrize("site,browser", [("hackernews", False), ("bilibili", True)])
def test_browser_defaults_do_not_reach_api_only_commands(tmp_path, monkeypatch, site, browser):
    (tmp_path / "cli-manifest.json").write_text(json.dumps([
        {"site": site, "name": "read", "browser": browser}
    ]))
    monkeypatch.setattr(managed_tools, "node_dir", lambda: tmp_path)
    monkeypatch.setattr(managed_tools, "managed_package_root", lambda: tmp_path)
    command = managed_tools.managed_command([site, "read"])
    assert ("--site-session" in command) is browser
    explicit = managed_tools.managed_command([site, "read", "--site-session", "persistent"])
    assert explicit.count("--site-session") == 1


def test_xiaoe_ephemeral_dispatch_uses_the_pinned_string_boolean_abi(tmp_path):
    from openmd.runtime.interpreters import run_interruptibly
    node_dir = managed_tools.node_dir()
    if node_dir is None:
        pytest.skip("Node not installed")
    host = tmp_path / "host"
    module = host / "dist/src"
    module.mkdir(parents=True)
    (host / "package.json").write_text('{"type":"module"}')
    (module / "registry.js").write_text("export const registry=new Map();export const getRegistry=()=>registry;")
    (module / "registry-api.js").write_text("import{registry}from'./registry.js';export const Strategy={COOKIE:'cookie'};export function cli(c){registry.set(c.site+'/'+c.name,c);return c;}")
    (module / "errors.js").write_text("export class ArgumentError extends Error{};export class CommandExecutionError extends Error{};export class EmptyResultError extends Error{};")
    (module / "execution.js").write_text("export async function executeCommand(c,a,d,o){if(o.keepTab!=='false'||o.siteSession!=='ephemeral')throw Error('pinned option ABI mismatch');return {fixture:true};}")
    dispatcher = Path(__file__).parents[1] / "integrations/opencli/xiaoe/dispatch.js"
    result = run_interruptibly([str(node_dir / ("node.exe" if os.name == "nt" else "node")), str(dispatcher), str(host), "content", "https://example.org"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"fixture": True}


def test_xiaohongshu_assets_bypass_path_shim_before_string_quoting(monkeypatch):
    from openmd.backends import xiaohongshu_providers
    monkeypatch.setattr(managed_tools, "managed_command", lambda args: ["pinned-node", "main.js", *args])
    monkeypatch.setattr(xiaohongshu_providers, "_resolve_executable", lambda name: "fake-opencli.cmd")
    runner = Mock(return_value=Mock(returncode=0, stdout="[]", stderr=""))
    monkeypatch.setattr(xiaohongshu_providers, "run_interruptibly", runner)
    xiaohongshu_providers._run_asset_command(["opencli", "openmd-xiaohongshu", "note-assets", "fixture"], 5, None)
    assert runner.call_args.args[0][:2] == ["pinned-node", "main.js"]
    assert runner.call_args.kwargs["env"]["OPENCLI_CONFIG_DIR"] == str(managed_tools.config_directory())


def test_quiet_browser_and_adapter_surfaces_share_one_container(tmp_path):
    from openmd.runtime.interpreters import run_interruptibly
    node = managed_tools.node_dir()
    if node is None:
        pytest.skip("Node not installed")
    source = (Path(__file__).parents[1] / "src/openmd/browser_extension/vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    function = "function getOwnedWindowRole(" + source.split("function getOwnedWindowRole(")[1].split("function getWindowRole(")[0]
    script = tmp_path / "roles.cjs"
    script.write_text("const assert=require('node:assert/strict');let currentContextId='openmd-a1b2c3d4';const openmdOptions={show:false};const getSurfaceFromKey=k=>k;" + function + "assert.equal(getOwnedWindowRole('browser'),'automation');assert.equal(getOwnedWindowRole('adapter'),'automation');openmdOptions.show=true;assert.equal(getOwnedWindowRole('browser'),'interactive');currentContextId='other';openmdOptions.show=false;assert.equal(getOwnedWindowRole('browser'),'interactive');")
    result = run_interruptibly([str(node / ("node.exe" if os.name == "nt" else "node")), str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
