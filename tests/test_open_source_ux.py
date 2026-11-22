from __future__ import annotations

from pathlib import Path

import pytest

from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import ContentItem, TranscriptResult


def _weixin(text: str, title: str = "Weixin Official Accounts Platform") -> tuple[TranscriptResult, ContentItem]:
    result = TranscriptResult(text=text, status="verified", source_level="third_party_extractor", backend="web.trafilatura", title=title)
    item = ContentItem(platform="weixin", input_type="single", url="https://mp.weixin.qq.com/s/abc")
    return result, item


# Observed 2026-11-15 on a fresh install: an invalid mp.weixin.qq.com link was
# saved as "已取得正文" with this body.
OBSERVED_PARAMETER_ERROR = (
    "Parameter error\n\n: ， ， ， ， ， ， .   Video Mini Program Like ，轻点两下取消赞 "
    "Wow ，轻点两下取消在看 Share Comment Favorite 听过\n"
)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (OBSERVED_PARAMETER_ERROR, "weixin_error_page"),
        ("参数错误\n", "weixin_error_page"),
        ("该内容已被发布者删除\n\n轻点两下取消赞", "weixin_error_page"),
        ("此内容因违规无法查看\n", "weixin_error_page"),
        ("链接已过期\n", "weixin_error_page"),
        ("环境异常\n\n当前环境异常，完成验证后即可继续访问。\n\n去验证", "browser_challenge_page"),
    ],
)
def test_weixin_error_pages_are_not_saved_as_content(body: str, code: str) -> None:
    result, item = _weixin(body)
    checked = validate_extraction_content(result, item)
    assert checked.status == "failed"
    assert checked.error_code == code
    assert checked.text == ""


def test_weixin_article_quoting_error_phrase_is_kept() -> None:
    body = "# 如何排查参数错误\n\n" + "接口返回 Parameter error 时，先检查签名和时间戳是否一致。\n\n" * 30
    result, item = _weixin(body, title="如何排查参数错误")
    assert validate_extraction_content(result, item).status == "verified"


def test_stable_extension_copy_keeps_manifest_and_id(tmp_path) -> None:
    from openmd import browser_setup

    folder = browser_setup.stable_extension_dir(tmp_path)
    assert (folder / "manifest.json").is_file() and (folder / "background.js").is_file()
    assert not list(folder.rglob("*.py"))
    assert browser_setup.extension_id(folder / "manifest.json") == browser_setup.extension_id()
    # Rerunning after an upgrade refreshes in place.
    assert browser_setup.stable_extension_dir(tmp_path) == folder


def test_doctor_human_view_lists_install_hints() -> None:
    from openmd.doctor_view import render

    payload = {"profile_diagnostics": {"asr_runtime": {"probe": {"ready": False}}, "tools": {"tools": [{"tool": "yutto", "resolved": False}]}}}
    text = render(payload)
    assert "openmd-cli[asr]" in text and "可选工具 yutto" in text and "openmd doctor --json" in text
    assert "{" not in text


def test_doctor_stays_json_when_not_a_terminal(capsys) -> None:
    import json

    from openmd.cli import main

    main(["doctor", "--profile", "core"])
    assert json.loads(capsys.readouterr().out)["command"] == "doctor"


def test_doctor_json_reports_managed_tool_origin(capsys, monkeypatch) -> None:
    import json

    from openmd.cli import main
    from openmd.runtime import managed_tools

    monkeypatch.setattr(managed_tools, "status", lambda: {"opencli": "managed", "opencli_version_pinned": "2.0.3"})
    main(["doctor", "--profile", "core", "--json"])
    assert json.loads(capsys.readouterr().out)["managed_tools"] == {
        "opencli": "managed", "opencli_version_pinned": "2.0.3",
    }


def test_source_verdict_outranks_later_missing_tool() -> None:
    from dataclasses import replace as _replace

    from openmd.backends.executor import BackendExecutor

    class Provider:
        def __init__(self, value: TranscriptResult) -> None:
            self.value = value

        def extract(self, request):
            return _replace(self.value, backend=request.primary_backend)

    error_page = TranscriptResult(text="Parameter error\n", status="verified", source_level="third_party_extractor", backend="x")
    missing = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="x", error_code="backend_exit_127")
    item = ContentItem(platform="weixin", input_type="single", url="https://mp.weixin.qq.com/s/abc")
    extracted = BackendExecutor(providers={"reader": Provider(error_page), "opencli": Provider(missing)}).extract(item, ("reader", "opencli"))
    assert extracted.error_code == "weixin_error_page"
    assert [a["backend"] for a in extracted.metadata["fallback_attempts"]] == ["reader", "opencli"]


def test_weixin_local_reader_names_error_page() -> None:
    from openmd.backends.base import BackendExtractRequest
    from openmd.backends.weixin import WeixinArticleLocalProvider

    shell = "<html><body><div class='weui-msg'><p>参数错误</p></div><span>，轻点两下取消赞</span><script>var a='x'</script></body></html>"
    provider = WeixinArticleLocalProvider(fetcher=lambda url, timeout: shell)
    item = ContentItem(platform="weixin", input_type="single", url="https://mp.weixin.qq.com/s/abc")
    result = provider.extract(BackendExtractRequest(item=item, backend_chain=("weixin.article.local",)))
    assert result.error_code == "weixin_error_page"


def test_failure_hint_names_opencli_when_it_is_the_missing_piece() -> None:
    from openmd import interactive

    rows = [{"event_type": "item", "error_code": "bilibili_subtitle_missing", "route_trace": {"fallback_attempts": [
        {"backend": "opencli.bilibili.subtitle", "error_code": "backend_not_found"},
        {"backend": "bilibili-harvest.core", "error_code": "bilibili_subtitle_missing"},
    ]}}]
    assert "OPENMD_NPM_REGISTRY" in interactive._failure_hint(rows)
    line = interactive.issue_context("https://www.bilibili.com/video/BV1x", rows)
    assert "bilibili-harvest.core=bilibili_subtitle_missing" in line and "browser.bilibili.subtitle" in line and "opencli" not in line


def test_issue_context_keeps_every_attempt() -> None:
    from openmd import interactive

    attempts = [{"backend": f"route.{i}", "error_code": "backend_exit_127"} for i in range(8)]
    line = interactive.issue_context("https://example.test/a", [{"event_type": "item", "route_trace": {"fallback_attempts": attempts}}])
    assert "route.7=backend_exit_127" in line and "truncated" not in line


def test_bridge_not_connected_points_to_the_opencli_extension() -> None:
    from openmd import interactive

    rows = [{"event_type": "item", "error_code": "backend_exit_69", "route_trace": {"fallback_attempts": [
        {"backend": "opencli.bilibili.subtitle", "error_code": "backend_exit_69"}]}}]
    hint = interactive._failure_hint(rows)
    assert "openmd browser-setup" in hint and "opencli" not in hint.lower()


def _fake_node(tmp_path, monkeypatch):
    import os

    from openmd.runtime import managed_tools

    node = tmp_path / "node"
    node.mkdir()
    (node / ("node.exe" if os.name == "nt" else "node")).write_text("", encoding="utf-8")
    npm = tmp_path / "npm-cli.js"
    npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(managed_tools, "node_dir", lambda: node)
    monkeypatch.setattr(managed_tools, "_npm_cli", lambda _node: npm)
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(managed_tools.shutil, "which", lambda name, path=None: None)
    return managed_tools, node


def test_managed_opencli_installs_once_pinned_and_activates(tmp_path, monkeypatch) -> None:
    import os
    import subprocess

    managed_tools, node = _fake_node(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "1")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        prefix = Path(command[command.index("--prefix") + 1])
        (prefix / "node_modules" / ".bin").mkdir(parents=True)
        (prefix / "node_modules" / ".bin" / ("opencli.cmd" if os.name == "nt" else "opencli")).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(managed_tools, "run_interruptibly", fake_run)
    messages = []
    assert managed_tools.ensure_opencli(messages.append) == "managed"
    assert f"@jackwener/opencli@{managed_tools.OPENCLI_VERSION}" in calls[0]
    assert str(managed_tools.managed_bin()) in os.environ["PATH"].split(os.pathsep)[:2]
    assert managed_tools.ensure_opencli(messages.append) == "managed" and len(calls) == 1


def test_managed_opencli_retries_then_falls_back_from_npm_mirror(tmp_path, monkeypatch) -> None:
    import os
    import subprocess

    managed_tools, _ = _fake_node(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "1")
    monkeypatch.setenv("OPENMD_NPM_REGISTRY", "https://registry.npmmirror.com")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) < 4:
            return subprocess.CompletedProcess(command, 1, "", "network unavailable")
        prefix = Path(command[command.index("--prefix") + 1])
        (prefix / "node_modules" / ".bin").mkdir(parents=True)
        (prefix / "node_modules" / ".bin" / ("opencli.cmd" if os.name == "nt" else "opencli")).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(managed_tools, "run_interruptibly", fake_run)
    assert managed_tools.ensure_opencli(lambda _m: None) == "managed"
    registries = [command[command.index("--registry") + 1] for command in calls]
    assert registries == ["https://registry.npmmirror.com"] * 3 + ["https://registry.npmjs.org"]


def test_managed_opencli_respects_opt_out_and_failed_install(tmp_path, monkeypatch) -> None:
    import subprocess

    managed_tools, _ = _fake_node(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "0")
    monkeypatch.setattr(managed_tools, "run_interruptibly", lambda *a, **k: pytest.fail("must not install"))
    assert managed_tools.ensure_opencli(lambda _m: None) == "missing"

    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "1")
    monkeypatch.setattr(managed_tools, "run_interruptibly", lambda command, **k: subprocess.CompletedProcess(command, 1, "", "npm ERR! network"))
    messages = []
    assert managed_tools.ensure_opencli(messages.append) == "missing"
    assert "OPENMD_NPM_REGISTRY" in messages[-1]
    assert not managed_tools.managed_root().exists()


def test_user_opencli_is_ignored_when_managed_missing(tmp_path, monkeypatch) -> None:
    from openmd.runtime import managed_tools

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(managed_tools.shutil, "which", lambda name, path=None: str(tmp_path / "global" / "opencli"))
    monkeypatch.setattr(managed_tools, "run_interruptibly", lambda *a, **k: pytest.fail("must not install"))
    assert managed_tools.activate() == "missing"


def test_managed_opencli_prepares_bundled_node_host_package(tmp_path, monkeypatch) -> None:
    import os

    from openmd.runtime import managed_tools

    node = tmp_path / "node"
    node.mkdir()
    (node / ("node.exe" if os.name == "nt" else "node")).write_text("", encoding="utf-8")
    managed_root = tmp_path / "data" / "tools" / "opencli"
    (managed_root / "node_modules" / ".bin").mkdir(parents=True)
    (managed_root / "node_modules" / ".bin" / ("opencli.cmd" if os.name == "nt" else "opencli")).write_text("", encoding="utf-8")
    (managed_root / "node_modules" / "@jackwener" / "opencli").mkdir(parents=True)

    calls = []
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(managed_tools, "node_dir", lambda: node)
    monkeypatch.setattr(managed_tools.shutil, "which", lambda name, path=None: str(managed_tools.managed_bin() / ("opencli.cmd" if os.name == "nt" else "opencli")))
    monkeypatch.setattr(Path, "symlink_to", lambda self, target, target_is_directory=False: calls.append((self, target, target_is_directory)))

    assert managed_tools.activate() == "managed"
    assert calls == [
        (
            node / "node_modules" / "@jackwener" / "opencli",
            managed_root / "node_modules" / "@jackwener" / "opencli",
            True,
        )
    ]


def test_plugin_setup_scripts_include_managed_opencli_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("setup-openmd-douyin-plugin.ps1", "setup-openmd-xiaohongshu-plugin.ps1"):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "OPENMD_DATA_DIR" in text
        assert "tools\\opencli\\node_modules\\.bin" in text


def test_host_package_symlink_permission_failure_uses_junction(tmp_path, monkeypatch):
    managed_tools, node = _fake_node(tmp_path, monkeypatch)
    source = managed_tools.managed_package_root()
    source.mkdir(parents=True)
    calls = []

    def denied(*args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(managed_tools, "_create_junction", lambda src, dst: calls.append((src, dst)))
    managed_tools._prepare_bundled_node_host_package()
    assert calls == [(source, node / "node_modules/@jackwener/opencli")]


def test_host_package_replaces_old_junction_without_deleting_source(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace

    managed_tools, node = _fake_node(tmp_path, monkeypatch)
    source = managed_tools.managed_package_root()
    source.mkdir(parents=True)
    target = node / "node_modules/@jackwener/opencli"
    target.mkdir(parents=True)  # emulate a junction whose is_symlink() is False
    old = tmp_path / "old-package"
    old.mkdir()
    marker = old / "keep.txt"
    marker.write_text("must remain", encoding="utf-8")
    original_lstat, original_resolve = Path.lstat, Path.resolve
    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(st_reparse_tag=getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)) if self == target else original_lstat(self))
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "resolve", lambda self, **kwargs: old if self == target else original_resolve(self, **kwargs))
    calls = []
    monkeypatch.setattr(Path, "symlink_to", lambda self, src, **kwargs: calls.append((self, src)))
    managed_tools._prepare_bundled_node_host_package()
    assert calls == [(target, source)]
    assert not target.exists() and marker.read_text(encoding="utf-8") == "must remain"


def test_host_package_keeps_real_directory(tmp_path, monkeypatch):
    managed_tools, node = _fake_node(tmp_path, monkeypatch)
    managed_tools.managed_package_root().mkdir(parents=True)
    target = node / "node_modules/@jackwener/opencli"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("real package", encoding="utf-8")
    monkeypatch.setattr(Path, "symlink_to", lambda *a, **k: pytest.fail("must not replace real package"))
    managed_tools._prepare_bundled_node_host_package()
    assert marker.read_text(encoding="utf-8") == "real package"


def test_host_package_failure_warns_once(tmp_path, monkeypatch, capsys):
    managed_tools, _ = _fake_node(tmp_path, monkeypatch)
    managed_tools.managed_package_root().mkdir(parents=True)
    monkeypatch.setattr(managed_tools, "_host_package_warning_shown", False)

    def denied(*args, **kwargs):
        raise OSError(1314, "denied")

    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(managed_tools, "_create_junction", denied)
    managed_tools._prepare_bundled_node_host_package()
    managed_tools._prepare_bundled_node_host_package()
    warning = capsys.readouterr().err
    assert warning.count("Openmd:") == 1
    assert "plugin" in warning and "opencli" not in warning.lower()


@pytest.mark.parametrize(("profiles", "preset", "expected"), [
    (["openmd", "x8k2m4pq"], None, "openmd"),   # both bridges: prefer the built-in one
    (["openmd"], None, None),                   # single bridge: opencli picks it itself
    (["x8k2m4pq"], None, None),                 # only the standalone OpenCLI extension
    (["openmd", "x8k2m4pq"], "work", None),     # a user's explicit choice wins
])
def test_bridge_profile_selection(monkeypatch, profiles, preset, expected) -> None:
    from openmd.runtime import services

    monkeypatch.setattr(services, "_bridge_profiles", lambda timeout=1.0: profiles)
    monkeypatch.setattr(services, "_opencli_launch", lambda command, env=None: (["node", "main.js"], ["node", "daemon.js"]))
    env = {"PATH": "x"} if preset is None else {"PATH": "x", "OPENCLI_PROFILE": preset}
    selected = services.bridge_profile_env(["opencli", "bilibili", "subtitle"], env)
    assert (selected or {}).get("OPENCLI_PROFILE") == expected


def test_vendored_bridge_is_patched_licensed_and_packaged() -> None:
    import json

    root = Path(__file__).resolve().parents[1]
    extension = root / "src/openmd/browser_extension"
    bridge = (extension / "vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    assert 'const generated = "openmd";' in bridge and "generateContextId();" not in bridge.split("function generateContextId")[0].split("const generated")[1][:40]
    assert "chrome.runtime.getManifest().version" not in bridge
    assert "Apache License" in (extension / "vendor/opencli-bridge/LICENSE").read_text(encoding="utf-8")
    assert (extension / "idle.html").is_file()
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["background"] == {"service_worker": "background.js", "type": "module"}
    assert {"debugger", "tabs", "cookies"} <= set(manifest["permissions"]) and manifest["host_permissions"] == ["<all_urls>"]
    assert (extension / "background.js").read_text(encoding="utf-8").count('import "./vendor/opencli-bridge/background.js";') == 1
    # tomllib is 3.11+; CI also runs 3.10.
    assert '"browser_extension/vendor/opencli-bridge/*"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_stable_extension_copy_includes_bridge(tmp_path) -> None:
    from openmd import browser_setup

    folder = browser_setup.stable_extension_dir(tmp_path)
    assert (folder / "vendor/opencli-bridge/background.js").is_file() and (folder / "idle.html").is_file()


def test_user_facing_text_never_names_opencli(tmp_path, monkeypatch) -> None:
    from openmd import doctor_view
    from openmd.runtime import managed_tools

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    text = doctor_view.render({"profile_diagnostics": {}})
    assert "opencli" not in text.lower()
    bridge = (Path(__file__).resolve().parents[1] / "src/openmd/browser_extension/vendor/opencli-bridge/background.js").read_text(encoding="utf-8")
    assert '"OpenCLI Browser"' not in bridge and 'chrome.windows.update(win.id, { state: "minimized" })' in bridge
    idle = (Path(__file__).resolve().parents[1] / "src/openmd/browser_extension/idle.html").read_text(encoding="utf-8")
    assert "OpenCLI" not in idle
    monkeypatch.delenv("OPENCLI_WINDOW", raising=False)
    monkeypatch.delenv("OPENMD_SHOW_BROWSER", raising=False)
    managed_tools.activate()
    import os
    assert os.environ["OPENCLI_WINDOW"] == "background"


@pytest.mark.parametrize("script", ["dev_setup.py", "vendor_opencli_bridge.py"])
def test_contributor_scripts_compile(script) -> None:
    import py_compile

    py_compile.compile(str(Path(__file__).resolve().parents[1] / "scripts" / script), doraise=True)


def test_failure_hint_surfaces_provider_next_step_for_new_failure_codes() -> None:
    # Round13 final acceptance: Xiaohongshu / Douyin providers wrote specific
    # guidance, but users saw only the generic "no saveable body" line and the
    # JSON receipt had next_action=null.
    from openmd import interactive
    from openmd.cli_login import source_next_action

    for code in ("xiaohongshu_signed_url_required", "opencli_xiaohongshu_timeout"):
        action = source_next_action(code)
        assert action and "扩展" in action and "--as-snapshot" in action
        assert interactive._failure_hint([{"error_code": code}]) == action
    for code in ("douyin_asr_unavailable", "asr_model_missing"):
        action = source_next_action(code)
        assert action and "openmd models install asr:baseline" in action
        assert interactive._failure_hint([{"error_code": code}]) == action
    action = source_next_action("douyin_detail_read_failed")
    assert action and "抖音" in action and "browser-setup" in action
    assert interactive._failure_hint([{"error_code": "douyin_detail_read_failed"}]) == action


def test_go_json_next_action_comes_from_the_row_error_code():
    """Round14: douyin_detail_read_failed printed a hint but left JSON next_action empty."""
    from types import SimpleNamespace
    from openmd.cli_login import source_next_action
    from openmd.interactive import _go_receipt_fields

    result = SimpleNamespace(next_action=None, error_code=None, status="failed")
    rows = [{"event_type": "item", "error_code": "douyin_detail_read_failed", "status": "failed"}]
    fields = _go_receipt_fields(result, rows, ok=False)
    assert fields["error_code"] == "douyin_detail_read_failed"
    assert fields["next_action"] == source_next_action("douyin_detail_read_failed")
    assert fields["next_action"]


def test_damaged_local_document_names_its_type_and_suggests_redownload():
    """Round14: a truncated EPUB was reported as '平台 pdf' with no next step."""
    from openmd.cli_login import source_next_action
    from openmd.interactive import issue_context

    rows = [{"event_type": "item", "error_code": "document_invalid_container", "backend": "document.engines"}]
    line = issue_context(r"C:\books\sample.epub", rows)
    assert "本地文件 .epub" in line and "平台 pdf" not in line
    assert "重新下载" in source_next_action("document_invalid_container")
