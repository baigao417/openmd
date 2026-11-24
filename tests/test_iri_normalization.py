from __future__ import annotations

import pytest

from openmd.core.input_validation import normalize_http_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://zh.wikipedia.org/wiki/中华人民共和国省级行政区?q=中文#片段",
            "https://zh.wikipedia.org/wiki/%E4%B8%AD%E5%8D%8E%E4%BA%BA%E6%B0%91%E5%85%B1%E5%92%8C%E5%9B%BD%E7%9C%81%E7%BA%A7%E8%A1%8C%E6%94%BF%E5%8C%BA?q=%E4%B8%AD%E6%96%87#%E7%89%87%E6%AE%B5",
        ),
        ("https://例子.测试:8443/a%20b/%zz/%?x=%2f&y=保留!$&'()*+,;=:@/?#中文", "https://xn--fsqu00a.xn--0zwm56d:8443/a%20b/%25zz/%25?x=%2f&y=%E4%BF%9D%E7%95%99!$&'()*+,;=:@/?#%E4%B8%AD%E6%96%87"),
        ("https://user:pass@[2001:db8::1]:8080/a", "https://user:pass@[2001:db8::1]:8080/a"),
        ("https://example.org:080/a", "https://example.org:080/a"),
        ("https://example.org/path?#", "https://example.org/path?#"),
    ],
)
def test_normalize_http_url_iri_components(source: str, expected: str) -> None:
    normalized = normalize_http_url(source)
    assert normalized == expected
    assert normalize_http_url(normalized) == normalized
    assert normalized.isascii()


@pytest.mark.parametrize("value", ["wechat://article/中文", "C:/資料/page.html", "relative/中文"])
def test_normalize_http_url_leaves_non_http_inputs_unchanged(value: str) -> None:
    assert normalize_http_url(value) == value


def test_batch_manifest_normalizes_each_url_before_identity(tmp_path) -> None:
    from openmd.cli import _read_urls_file

    manifest = tmp_path / "urls.txt"
    manifest.write_text("https://example.org/中文\nhttps://example.org/%E4%B8%AD%E6%96%87\n", encoding="utf-8")
    urls = _read_urls_file(str(manifest))
    assert urls == ["https://example.org/%E4%B8%AD%E6%96%87"] * 2


def test_mcp_url_validation_uses_shared_normalizer():
    from openmd.mcp_jobs import _validate_source_url

    assert _validate_source_url("https://example.org/中文?q=测试") == (
        "https://example.org/%E4%B8%AD%E6%96%87?q=%E6%B5%8B%E8%AF%95"
    )


def test_normalized_and_iri_urls_share_package_and_compact_identity():
    from types import SimpleNamespace

    from openmd.cli import _compact_item_identity, _compact_url_identity
    from openmd.core.package_contract import normalized_input_ref

    iri = "https://example.org/wiki/中文"
    uri = "https://example.org/wiki/%E4%B8%AD%E6%96%87"
    assert normalized_input_ref(SimpleNamespace(url=iri)) == normalized_input_ref(SimpleNamespace(url=uri))
    assert _compact_url_identity(iri) == _compact_url_identity(uri)
    assert _compact_item_identity(SimpleNamespace(platform="web", url=iri)) == _compact_item_identity(
        SimpleNamespace(platform="web", url=uri)
    )


def test_renderer_persists_normalized_url_and_not_original_iri():
    from openmd.core.models import ContentItem, TranscriptResult
    from openmd.core.renderer import render_markdown_document

    item = ContentItem(
        platform="web", input_type="single", url="https://example.org/wiki/中文",
        title="Example", source_type="article",
    )
    result = TranscriptResult(text="Example body", status="verified", source_level="native_text", backend="mock")
    document = render_markdown_document(item, result)
    assert document.frontmatter["url"] == "https://example.org/wiki/%E4%B8%AD%E6%96%87"
    assert "中文" not in document.content


def test_redirect_location_is_normalized_before_following():
    import email.message
    import urllib.request

    from openmd.backends.web_article import _NormalizedRedirects

    headers = email.message.Message()
    request = urllib.request.Request("https://example.org/start")
    redirected = _NormalizedRedirects().redirect_request(
        request, None, 302, "Found", headers, "https://example.org/中文?q=词"
    )
    assert redirected is not None
    assert redirected.full_url == "https://example.org/%E4%B8%AD%E6%96%87?q=%E8%AF%8D"


@pytest.mark.parametrize("command", ["ingest", "batch"])
def test_single_item_commands_normalize_before_adapter(command: str, tmp_path, monkeypatch) -> None:
    from openmd import cli
    from openmd.adapters.registry import BackendSelection

    observed = []

    class Adapter:
        id = "web_article"

        def resolve_items(self, input_url, *, limit=None, all=False):
            observed.append(input_url)
            raise RuntimeError("adapter_input_observed")

    class Registry:
        adapter = Adapter()

        def adapter_for(self, value):
            return self.adapter

        def select_backend(self, platform, requested=None, *, allow_fallback=True):
            return BackendSelection(platform=platform, primary="mock", fallbacks=())

    monkeypatch.setattr(cli, "create_default_registry", lambda: Registry())
    args = cli.build_parser().parse_args(["ingest", "https://example.org/中文", "--backend", "mock", "--out", str(tmp_path)])
    with pytest.raises(RuntimeError, match="adapter_input_observed"):
        cli._process_input("https://example.org/中文", args, command=command)
    assert observed == ["https://example.org/%E4%B8%AD%E6%96%87"]


def test_discovered_child_urls_normalize_before_collection_identity(monkeypatch):
    from openmd import cli
    from openmd.core.models import ContentItem

    child = ContentItem(
        platform="web_article", input_type="single", url="https://example.org/子页面", source_type="article"
    )
    observed = []

    class Observed(Exception):
        pass

    class Registry:
        def select_backend_for_item(self, item, requested, *, allow_fallback):
            observed.append(item.url)
            raise Observed

    monkeypatch.setattr(cli, "create_default_registry", lambda: Registry())
    with pytest.raises(Observed):
        cli._process_items([child], type("Args", (), {"fallback": True, "backend": None})(), command="collect")
    assert observed == ["https://example.org/%E5%AD%90%E9%A1%B5%E9%9D%A2"]
    assert child.url == "https://example.org/子页面"


def test_snapshot_url_is_normalized_before_snapshot_item(monkeypatch, tmp_path):
    from openmd import cli

    source = tmp_path / "snapshot.html"
    source.write_text("<html><body>synthetic</body></html>", encoding="utf-8")
    args = cli.build_parser().parse_args([
        "go", str(source), "--as-snapshot", "--url", "https://example.org/快照",
        "--backend", "mock", "-o", str(tmp_path / "out"),
    ])

    class Observed(Exception):
        pass

    def capture(_path, url, **_kwargs):
        assert url == "https://example.org/%E5%BF%AB%E7%85%A7"
        raise Observed

    monkeypatch.setattr("openmd.backends.loaded_chat.snapshot_item", capture)
    with pytest.raises(Observed):
        cli._process_input(str(source), args, command="go")


def test_web_article_encoding_exception_is_a_bounded_route_failure(monkeypatch):
    import urllib.request

    from openmd.backends.web_article import WebFetchError, _fetch_web_html

    class Opener:
        def open(self, *_args, **_kwargs):
            raise UnicodeEncodeError("ascii", "中文", 0, 1, "ordinal not in range")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: Opener())
    with pytest.raises(WebFetchError) as caught:
        _fetch_web_html("https://example.org/中文", sleep=lambda *_args: None)
    assert caught.value.code == "web_fetch_url_encoding_error"
    assert caught.value.attempts == [{"route": "proxy", "outcome": "web_fetch_url_encoding_error", "http_status": None}]


def test_go_normalizes_before_network_preflight(monkeypatch, tmp_path):
    from openmd import cli, interactive
    from openmd.core import input_validation

    observed = []
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    monkeypatch.setattr(input_validation, "network_preflight", lambda url: observed.append(url) or "synthetic stop")
    code = cli.main([
        "go", "https://example.org/中文", "--yes",
        "--enable-network-backends", "--save-to", str(tmp_path),
    ])
    assert code == 1
    assert observed == ["https://example.org/%E4%B8%AD%E6%96%87"]


def test_collect_normalizes_discovery_url_before_platform_detection(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from openmd import cli

    observed = []

    class Observed(Exception):
        pass

    def detect(value):
        observed.append(value)
        raise Observed

    monkeypatch.setattr(cli, "_start_compact_receipt", lambda *_args: None)
    monkeypatch.setattr(cli, "_read_single_input", lambda value: value)
    monkeypatch.setattr(cli, "_detect_collect_platform", detect)
    args = SimpleNamespace(url="https://example.org/合集/中文", all=False, receipt=None, out=str(tmp_path))
    with pytest.raises(Observed):
        cli.cmd_collect(args)
    assert observed == ["https://example.org/%E5%90%88%E9%9B%86/%E4%B8%AD%E6%96%87"]


def test_mcp_openmd_save_normalizes_before_queue(tmp_path, monkeypatch):
    from openmd import mcp_jobs

    observed = []
    monkeypatch.setattr(mcp_jobs.host, "jobs_root", lambda: tmp_path / "jobs")
    monkeypatch.setattr(mcp_jobs.host, "_write_status", lambda _path, status: observed.append(status["url"]))
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)
    queued = mcp_jobs.save("https://example.org/中文", str(tmp_path / "out"))
    assert queued["state"] == "queued"
    assert observed and set(observed) == {"https://example.org/%E4%B8%AD%E6%96%87"}


def test_mcp_openmd_save_page_normalizes_source_url(tmp_path, monkeypatch):
    from openmd import mcp_jobs

    observed = []
    monkeypatch.setattr(
        mcp_jobs.host, "start_snapshot_job",
        lambda html, url, **_kwargs: observed.append(url) or {"id": "b" * 32, "state": "queued"},
    )
    monkeypatch.setattr(mcp_jobs.host, "_write_status", lambda *_args: None)
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)
    queued = mcp_jobs.save_page("https://example.org/页面", str(tmp_path / "out"), html="<html><body>ok</body></html>")
    assert queued["state"] == "queued"
    assert observed == ["https://example.org/%E9%A1%B5%E9%9D%A2"]
