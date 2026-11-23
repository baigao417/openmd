from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import BackendExecutor
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.models import ContentItem, TranscriptResult
from openmd.routing.loader import default_backend_chains, load_catalog, route_trace, validate_catalog
from openmd.routing.providers import CommandToolProvider, doctor_tools
from openmd.routing.readers import read_text, subtitle_text
from openmd.routing.runner import Launcher, ToolError, ToolRun, build_argv, isolated_environment, resolve_launcher, run_tool
from openmd.routing.view import render_routes_markdown


def _catalog_data():
    catalog = load_catalog()
    cards = copy.deepcopy(list(catalog.cards.values()))
    routes = {"schema": "openmd.routes/v1", "version": catalog.version, "routes": copy.deepcopy(list(catalog.routes)), "capabilities": []}
    return cards, routes


def _media_tool(card_id="test-media", platform="bilibili", backend_id=None, *, asr=True):
    backend_id = backend_id or f"{card_id}.{platform}.media"
    capability = {
        "id": backend_id, "card_id": card_id, "sources": [platform], "capability": "media",
        "argv": ["fake-tool", "{url}"], "reader": {"type": "media_dir"},
        "then": "asr.auto" if asr else None,
        "gates": ["network", "media_download", *( ["asr"] if asr else [])],
        "source_level": "media_asr" if asr else "media_download",
        "timeout_s": 30, "validation": {"min_chars": 1},
    }
    return ({"schema": "openmd.tool-card/v1", "id": card_id, "kind": "command",
             "version": "1", "license": "MIT", "homepage": "https://example.org",
             "installation": {"mode": "isolated_existing", "verification": "test fixture"},
             "evidence": [{"date": "2026-11-21", "ledger": "test", "scope": "synthetic"}],
             "launcher": {"which": ["fake-tool"]}, "capabilities": [capability]}, capability)


def test_route_reference_and_media_contract_rejected():
    cards, routes = _catalog_data()
    routes["routes"][0]["chain"].append("missing-tool")
    with pytest.raises(ValueError, match="route_backend_unknown"):
        validate_catalog(cards, routes)
    cards, routes = _catalog_data()
    card, _ = _media_tool()
    cards.append(card)
    routes["routes"].append({"source": "bilibili", "capability": "media", "chain": ["test-media.bilibili.media"]})
    card["capabilities"][0].pop("then")
    with pytest.raises(ValueError, match="tool_media_requires_asr"):
        validate_catalog(cards, routes)


def test_declarative_default_projection_and_generated_document(monkeypatch, tmp_path):
    config = tmp_path / "local.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "route_overlay": str(tmp_path / "absent.json")}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    chains = default_backend_chains()
    assert chains["web_article"][:2] == ("web_article.discourse", "defuddle.web.content")
    assert chains["bilibili"][0] == "opencli.bilibili.subtitle"
    assert chains["youtube"][0] == "opencli.youtube.transcript"
    assert "podwise.transcript" not in chains["xiaoyuzhou"]
    assert Path("docs/routes.generated.md").read_text(encoding="utf-8-sig").replace("\r\n", "\n") == render_routes_markdown()


def test_kaze_route_follows_chrome_by_default_and_is_first_when_preferred(monkeypatch):
    # Keep this projection test independent of the maintainer's route overlay.
    from tempfile import TemporaryDirectory

    import json
    from pathlib import Path

    with TemporaryDirectory() as tmp:
        config = Path(tmp) / "local.json"
        config.write_text(json.dumps({"schema": "openmd.local/v1"}), encoding="utf-8")
        monkeypatch.setenv("OPENMD_CONFIG", str(config))
        from openmd.routing.loader import load_catalog

        load_catalog.cache_clear()
        monkeypatch.delenv("OPENMD_KAZE", raising=False)
        chain = default_backend_chains()["web_article"]
        assert chain.index("opencli.web.read") < chain.index("browser.kaze.snapshot")
        x_chain = default_backend_chains()["x"]
        assert x_chain.index("opencli.twitter.thread") < x_chain.index("browser.kaze.snapshot")
        monkeypatch.setenv("OPENMD_KAZE", "1")
        assert default_backend_chains()["web_article"][0] == "browser.kaze.snapshot"
        assert default_backend_chains()["x"][0] == "browser.kaze.snapshot"


def test_command_tools_gated_and_explicit_override_preserved():
    disabled = create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions())
    assert not isinstance(disabled.provider_for("defuddle.web.content"), CommandToolProvider)
    enabled = create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions(enable_network_backends=True))
    assert isinstance(enabled.provider_for("defuddle.web.content"), CommandToolProvider)
    override = create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions(enable_network_backends=True, backend_override="web_article.trafilatura"))
    assert not isinstance(override.provider_for("defuddle.web.content"), CommandToolProvider)
    catalog = load_catalog()
    card, capability = _media_tool(asr=True)
    provider = CommandToolProvider(card, capability, BackendRuntimeOptions(enable_network_backends=True, enable_media_download_backends=True))
    result = provider.extract(BackendExtractRequest(ContentItem("bilibili", "single", "https://www.bilibili.com/video/BV1KLGE6FEoJ"), (provider.name,)))
    assert result.error_code == "tool_permission_required"


def test_url_is_one_argv_argument_and_unknown_placeholder_rejected(tmp_path):
    url = 'https://example.com/article?q=a&x="$(whoami)"'
    cap = {"argv": ["parse", "{url}", "--out", "{workdir}/result.md"]}
    argv = build_argv(Launcher((sys.executable, "tool.py"), "test", Path("tool.py")), cap, url=url, item_id=None, workdir=tmp_path)
    assert argv[3] == url
    assert len(argv) == 6
    with pytest.raises(ToolError, match="placeholder"):
        build_argv(Launcher((sys.executable,), "test", Path("tool.py")), {"argv": ["{unknown}"]}, url=url, item_id=None, workdir=tmp_path)
    with pytest.raises(ToolError):
        build_argv(Launcher((sys.executable,), "test", Path("tool.py")), cap, url="--help", item_id=None, workdir=tmp_path)


def test_child_environment_has_fresh_home_and_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_TOKEN", "private")
    monkeypatch.setenv("OPENMD_SOME_KEY", "private")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    env = isolated_environment(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path / ".home")
    assert env["HOME"] == env["XDG_CONFIG_HOME"]
    assert "SECRET_TOKEN" not in env and "OPENMD_SOME_KEY" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9999"
    assert "PYTHONPATH" not in env


def test_batch_launcher_rejected(monkeypatch, tmp_path):
    batch = tmp_path / "defuddle.cmd"
    batch.write_text("@echo off\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"defuddle": {"node": str(batch), "root": str(tmp_path)}}}), encoding="utf-8")
    entry = tmp_path / "defuddle/dist/cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    with pytest.raises(ToolError) as error:
        resolve_launcher(load_catalog().cards["defuddle"])
    assert error.value.code == "tool_launcher_unsafe"


def test_readers_reject_metadata_error_and_challenge(tmp_path):
    catalog = load_catalog()
    cap = catalog.capabilities["x-tweet-fetcher.x.content"]
    for payload in ({"tweet": {"likes": 1}}, {"error": "upstream", "tweet": {"text": "could be stale"}}):
        with pytest.raises(ToolError):
            read_text(cap, ToolRun(0, 1, json.dumps(payload), "", {}), tmp_path)
    web = catalog.capabilities["defuddle.web.content"]
    (tmp_path / "content.json").write_text(json.dumps({"title": "Just a moment", "content": "# Just a moment\n\nverify you are human\nPlease enable JavaScript before continuing to the site."}), encoding="utf-8")
    with pytest.raises(ToolError, match="challenge/login"):
        read_text(web, ToolRun(0, 1, "", "", {}), tmp_path)


def test_defuddle_actual_json_metadata_does_not_use_internal_heading(tmp_path):
    # defuddle 0.19.6 --markdown --json emits Markdown in content, with no
    # contentMarkdown key. Its first H1 may be a later internal section.
    text = "Classes provide a means of bundling data and functionality together.\n\n# Function defined outside the class\n\nOriginal section text."
    (tmp_path / "content.json").write_text(json.dumps({"title": "9. Classes", "content": text, "author": "Source Author", "published": "2026-11-07"}), encoding="utf-8")
    result = read_text(load_catalog().capabilities["defuddle.web.content"], ToolRun(0, 1, "", "", {}), tmp_path)
    assert result.text == text + "\n"
    assert result.title == "9. Classes"
    assert result.author == "Source Author" and result.published_at == "2026-11-07"


def test_defuddle_runner_parses_shared_html_with_node_api_without_network(monkeypatch, tmp_path):
    import openmd.routing.runner as runner
    from openmd.routing.runner import Launcher

    html = "<html><body><article>Shared source HTML for the Defuddle candidate.</article></body></html>"
    launcher = Launcher(
        ("node.exe", "-e", "existing wrapper", "defuddle-cli.js", "defuddle-node-api.js", "Defuddle", '{"removeExactSelectors":false}'),
        "managed", tmp_path / "defuddle-cli.js",
    )
    seen = {}

    class Process:
        returncode = 0

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        kwargs["stdout"].write(b'{"title":"Article","content":"Shared source HTML became Markdown with enough words to validate."}')
        kwargs["stderr"].write(b"")
        return Process()

    monkeypatch.setattr(runner, "resolve_launcher", lambda _card: launcher)
    monkeypatch.setattr(runner, "owned_popen", fake_popen)
    monkeypatch.setattr(runner, "wait_interruptibly", lambda _process, _timeout: None)
    item = ContentItem("web_article", "single", "https://example.test/article", raw_meta={"source_html": html})
    card = {"id": "defuddle"}
    capability = {
        "id": "defuddle.web.content", "argv": ["parse", "{url}"], "timeout_s": 10,
    }

    result = runner.run_tool(card, capability, item, tmp_path)

    assert seen["argv"][0:2] == ["node.exe", "-e"]
    assert "defuddle-node-api.js" in seen["argv"]
    assert str(tmp_path / "source.html") in seen["argv"]
    assert not (tmp_path / "source.html").exists()
    assert "<html>" not in result.stdout
    assert result.exit_code == 0


def test_overlay_updates_registry_capability_binding_gates_and_trace(monkeypatch, tmp_path, capsys):
    from openmd.adapters.registry import create_default_registry
    from openmd.core.capabilities import get_capability

    overlay = tmp_path / "overlay.jsonl"
    overlay.write_text(json.dumps({"schema": "openmd.route-overlay/v1", "op": "demote", "source": "x", "capability_id": "openmd.x.public", "ts": "2026-11-07T12:00:00+00:00"}) + "\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "route_overlay": str(overlay)}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    record = get_capability("x")
    assert record.planned_provider == record.runtime_provider == "openmd.x.logged_in_browser"
    assert record.required_gates == ("browser",)
    assert record.card_id == "builtin" and record.overlay_applied
    assert create_default_registry().backend_chain("x")[0] == record.runtime_provider
    assert route_trace("x", record.runtime_provider) == {"card_id": "builtin", "route_version": load_catalog().version, "overlay_applied": True}
    assert "openmd.x.public → openmd.x.logged_in_browser → opencli.twitter.thread" in render_routes_markdown()
    assert "openmd.x.logged_in_browser → opencli.twitter.thread → browser.kaze.snapshot → openmd.x.public" in render_routes_markdown(effective=True)
    from openmd.routing.view import main as view_main
    assert view_main(["--bundled", "--format", "markdown"]) == 0
    assert "openmd.x.public → openmd.x.logged_in_browser → opencli.twitter.thread" in capsys.readouterr().out


def test_tool_identity_mismatch_fails_before_execution(monkeypatch):
    import openmd.routing.providers as providers

    monkeypatch.setattr(providers, "installed_identity", lambda card: {"tool_version_verified": False})
    monkeypatch.setattr(providers, "run_tool", lambda *args: pytest.fail("mismatched tools must never run"))
    catalog = load_catalog()
    provider = CommandToolProvider(catalog.cards["defuddle"], catalog.capabilities["defuddle.web.content"], BackendRuntimeOptions(enable_network_backends=True))
    result = provider.extract(BackendExtractRequest(ContentItem("web_article", "single", "https://example.org/article"), (provider.name,)))
    assert not provider.health().ok and result.error_code == "tool_version_mismatch"


def test_media_card_composes_actual_media_with_asr_factory(monkeypatch):
    import openmd.routing.providers as providers
    import openmd.runtime.asr as asr

    seen = []
    monkeypatch.setattr(providers, "installed_identity", lambda card: {"tool_version_verified": True, "tool_version": "2.3.1"})
    def download(card, capability, item, workdir):
        (workdir / "Original video title.m4a").write_bytes(b"fake-audio-container")
        return ToolRun(0, 1.0, "", "", {})
    class ASR:
        def transcribe(self, path):
            seen.append((path.name, path.read_bytes()))
            return TranscriptResult(text="[00:00:01] Original spoken words.\n", status="verified", source_level="media_asr", backend="asr.auto", metadata={"asr_route": {"device": "cuda"}})
    monkeypatch.setattr(providers, "run_tool", download)
    monkeypatch.setattr(asr, "get_asr_backend", lambda name: ASR() if name == "asr.auto" else pytest.fail("wrong ASR factory"))
    catalog = load_catalog()
    card, capability = _media_tool(asr=True)
    provider = CommandToolProvider(card, capability, BackendRuntimeOptions(enable_network_backends=True, enable_media_download_backends=True, enable_asr_backends=True))
    result = provider.extract(BackendExtractRequest(ContentItem("bilibili", "single", "https://www.bilibili.com/video/BV1KLGE6FEoJ"), (provider.name,)))
    assert seen == [("Original video title.m4a", b"fake-audio-container")]
    assert result.status == "verified" and result.source_level == "media_asr" and result.backend == provider.name
    assert result.title == "Original video title"
    assert result.metadata["asr_route"]["device"] == "cuda" and result.metadata["tool_run"]["card_id"] == "test-media"
    assert result.text.startswith("[00:00:01]")


def test_subtitle_timestamps_remain():
    assert subtitle_text("1\n00:00:01,000 --> 00:00:02,300\nHello\n\n2\n00:01:02,000 --> 00:01:04,000\nworld") == "[00:00:01] Hello\n[00:01:02] world"


def test_runner_real_child_isolated_and_timeout(monkeypatch, tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import os,json; print(json.dumps({'home':os.environ['USERPROFILE'],'token':os.environ.get('SECRET_TOKEN')}))", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"fake": {"python": sys.executable, "script_root": str(tmp_path)}}}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    monkeypatch.setenv("SECRET_TOKEN", "hidden")
    card = {"id": "fake", "kind": "python_script", "launcher": {"script": "child.py"}}
    cap = {"argv": [], "timeout_s": 5}
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = run_tool(card, cap, ContentItem("web_article", "single", "https://example.com"), workdir)
    assert result.exit_code == 0
    child = json.loads(result.stdout)
    assert child["home"] == str(workdir / ".home") and child["token"] is None
    script.write_text("import time; time.sleep(15)", encoding="utf-8")
    cap["timeout_s"] = 0.1
    with pytest.raises(ToolError) as error:
        run_tool(card, cap, ContentItem("web_article", "single", "https://example.com"), workdir)
    assert error.value.code == "tool_timeout"


def test_declared_tool_missing_falls_through(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"defuddle": {"root": str(tmp_path / "missing")}}}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    providers = create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions(enable_network_backends=True))
    result = BackendExecutor(provider_registry=providers).extract(ContentItem("web_article", "single", "https://example.com"), ("defuddle.web.content", "mock"))
    assert result.backend == "mock" and result.status == "verified"
    assert any("tool_not_installed" in note for note in result.source_notes)


def test_tls_shim_overrides_downloader_disable(monkeypatch):
    import requests
    from openmd.routing.entry_shim import _require_tls_verification

    seen = []
    monkeypatch.setattr(requests.sessions.Session, "send", lambda self, req, **kwargs: seen.append(kwargs["verify"]))
    _require_tls_verification()
    session = requests.Session()
    session.verify = False
    session.send(object(), verify=False)
    assert seen == [True]


def test_yutto_native_tls_and_already_imported_alias_are_forced(monkeypatch):
    import types
    from openmd.routing.entry_shim import _require_yutto_tls_verification

    seen = []
    original = lambda *args, **kwargs: seen.append(kwargs["verify"])
    fetcher = types.ModuleType("yutto.utils.fetcher")
    fetcher.create_client = original
    alias = types.ModuleType("yutto.core.execution")
    alias.create_client = original
    monkeypatch.setitem(sys.modules, fetcher.__name__, fetcher)
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    _require_yutto_tls_verification()
    fetcher.create_client()
    alias.create_client(verify=False)
    assert seen == [True, True]


def test_python_console_script_cannot_bypass_isolated_shim(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1"}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    assert "yutto" not in load_catalog().cards


def test_unknown_and_local_gate_checks_never_raise():
    from openmd.routing.providers import gates_open

    assert gates_open({"gates": ["local_compute"]}, BackendRuntimeOptions())
    assert not gates_open({"gates": ["local_export"]}, BackendRuntimeOptions())
    assert not gates_open({"gates": ["future_gate"]}, BackendRuntimeOptions())
    assert set(load_catalog().capabilities["asr.fallback"]["gates"]) == {"network", "media_download", "asr"}


def test_cancel_kills_child_then_propagates_keyboard_interrupt(monkeypatch, tmp_path):
    import openmd.routing.runner as runner

    killed = []
    class InterruptedChild:
        def poll(self):
            raise KeyboardInterrupt()
    process = InterruptedChild()
    monkeypatch.setattr(runner, "resolve_launcher", lambda card: Launcher((sys.executable,), "unit", Path(sys.executable)))
    monkeypatch.setattr(runner, "owned_popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner, "_kill_tree", lambda child: killed.append(child))
    with pytest.raises(KeyboardInterrupt):
        run_tool({}, {"argv": [], "timeout_s": 1}, ContentItem("web_article", "single", "https://example.org/article"), tmp_path)
    assert killed == [process]


def test_repeated_optional_media_retention_keeps_success(monkeypatch, tmp_path):
    import openmd.routing.providers as providers
    import openmd.runtime.asr as asr

    monkeypatch.setattr(providers, "installed_identity", lambda card: {"tool_version_verified": True})
    def download(card, capability, item, workdir):
        (workdir / "audio.m4a").write_bytes(b"media")
        return ToolRun(0, 1, "", "", {})
    class ASR:
        def transcribe(self, path):
            return TranscriptResult(text="Original spoken words.\n", status="verified", source_level="media_asr", backend="asr.auto")
    monkeypatch.setattr(providers, "run_tool", download)
    monkeypatch.setattr(asr, "get_asr_backend", lambda name: ASR())
    catalog = load_catalog()
    options = BackendRuntimeOptions(enable_network_backends=True, enable_media_download_backends=True, enable_asr_backends=True, keep_media_artifacts=True, media_artifact_dir=str(tmp_path))
    card, capability = _media_tool(asr=True)
    provider = CommandToolProvider(card, capability, options)
    request = BackendExtractRequest(ContentItem("bilibili", "single", "https://www.bilibili.com/video/BV1KLGE6FEoJ"), (provider.name,))
    assert provider.extract(request).status == provider.extract(request).status == "verified"
    assert len(list(tmp_path.glob("*.m4a"))) == 2
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing user file", encoding="utf-8")
    provider.options = BackendRuntimeOptions(enable_network_backends=True, enable_media_download_backends=True, enable_asr_backends=True, keep_media_artifacts=True, media_artifact_dir=str(blocked))
    result = provider.extract(request)
    assert result.status == "verified" and result.metadata["media_retained"] is False
    assert blocked.read_text(encoding="utf-8") == "existing user file"


@pytest.mark.parametrize(("platform", "url", "media_kind", "suffix"), [
    ("bilibili", "https://www.bilibili.com/video/BV1abc123", "video", ".mp4"),
    ("xiaoyuzhou", "https://www.xiaoyuzhoufm.com/episode/684c1c3c4abe6e29cb000fc2", "audio", ".m4a"),
])
def test_media_route_download_only_skips_asr(monkeypatch, tmp_path, platform, url, media_kind, suffix):
    import openmd.routing.providers as providers

    card_id = f"test-{platform}"
    card, capability = _media_tool(card_id, platform, asr=False)
    monkeypatch.setattr(providers, "installed_identity", lambda _card: {"tool_version_verified": True})
    seen = []

    def download(_card, passed_capability, _item, workdir):
        seen.append(tuple(passed_capability["argv"]))
        (workdir / f"downloaded{suffix}").write_bytes(b"native-media")
        if media_kind == "video":
            (workdir / "audio.m4a").write_bytes(b"audio")
        return ToolRun(0, 1, "", "", {})

    monkeypatch.setattr(providers, "run_tool", download)
    output = tmp_path / "raw-media"
    result = CommandToolProvider(
        card,
        capability,
        BackendRuntimeOptions(enable_network_backends=True, enable_media_download_backends=True),
    ).download_only(ContentItem(platform, "single", url), output, media_kind=media_kind)

    assert isinstance(result, Path) and result.is_file() and result.suffix == suffix
    assert result.read_bytes() == b"native-media"
    assert len(seen) == 1
    assert seen[0] == ("fake-tool", "{url}")
