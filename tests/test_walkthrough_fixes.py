from __future__ import annotations

import socket
import time
import urllib.error
from dataclasses import replace

import pytest

from openmd import interactive
from openmd.cli import main
from openmd.core import content_validation, input_validation
from openmd.core.content_validation import html_tag_residue, validate_extraction_content
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.runlog import build_item_runlog_entry


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))


def article(text, backend="opencli.web.read"):
    return TranscriptResult(text=text, status="verified", source_level="third_party_extractor", backend=backend)


@pytest.mark.parametrize("backend", ["opencli.web.read", "web_article.trafilatura", "crawl4ai.web_article", "defuddle.web.content", "weixin.article.local"])
def test_all_web_results_are_normalized_by_shared_validator(backend):
    item = ContentItem(platform="weixin" if backend.startswith("weixin") else "web_article", input_type="single", url="https://example.test/article")
    result = validate_extraction_content(article("<table><tr><td><font>First paragraph.</font><br><br>Second paragraph.</td></tr></table>", backend), item)
    assert result.status == "verified"
    assert html_tag_residue(result.text) == (0, 0)
    assert "First paragraph." in result.text and "Second paragraph." in result.text
    assert "\n" in result.text.strip()


def test_conversion_preserves_markdown_code_and_existing_prose():
    text = '# Title\n\n**Bold** and `literal <br>`\n\n```html\n<table>example</table>\n```\n\n<p>A paragraph.</p>'
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(article(text), item)
    assert "**Bold**" in result.text
    assert "`literal <br>`" in result.text
    assert "```html\n<table>example</table>\n```" in result.text
    assert html_tag_residue(result.text) == (0, 0)


def test_layout_tables_do_not_collapse_article_into_one_markdown_cell():
    body = '<table width="100%"><tr><td><table><tr><td><font>First paragraph.</font><br><br>Second paragraph.</td></tr></table></td></tr></table>'
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(article(body), item)
    assert "|" not in result.text
    assert "First paragraph." in result.text and "Second paragraph." in result.text
    assert any(not line.strip() for line in result.text.strip().splitlines())


def test_data_tables_keep_their_rows_and_columns():
    body = '<table><tr><th>Item</th><th>Cost</th></tr><tr><td>A</td><td>10</td></tr></table>'
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(article(body), item)
    assert "| Item | Cost |" in result.text
    assert "| A | 10 |" in result.text


def test_residue_ignores_inline_and_matching_fences():
    text = '````html\n<br>\n```\n<table>\n````\n`<font>`\nreal <br>'
    assert html_tag_residue(text) == (1, 1)


def test_mixed_conversion_preserves_longer_closing_fence_and_multitick_inline():
    text = '```html\n<br>\n````\n\n``literal ` and <font>``\n\n<p>Real prose.</p>'
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(article(text), item)
    assert '```html\n<br>\n````' in result.text
    assert '``literal ` and <font>``' in result.text
    assert html_tag_residue(result.text) == (0, 0)


def test_residue_forces_partial_and_explains_runlog(monkeypatch):
    monkeypatch.setattr(content_validation, "html_to_markdown", lambda text: "Text <br> residue")
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(article("<p>Text</p>"), item)
    assert result.status == "partial"
    assert result.source_level == "third_party_extractor"
    row = build_item_runlog_entry(item, result)
    assert row["error_code"] == "html_tag_residue"
    assert "1 lines" in row["error_summary"]


def test_non_web_transcript_remains_literal():
    item = ContentItem(platform="youtube", input_type="single", source_type="video", url="https://example.test")
    result = validate_extraction_content(article("<font>spoken words</font>"), item)
    assert result.text == "<font>spoken words</font>"


def test_browser_verification_shell_is_not_source_content():
    item = ContentItem(platform="web_article", input_type="single", url="https://linux.do/t/3148150")
    result = validate_extraction_content(replace(article("Verification successful. Waiting for linux.do to respond"), title="Just a moment..."), item)
    assert result.status == "failed"
    assert result.error_code == "browser_challenge_page"
    assert not result.text and not result.components
    assert "被站点验证页拦截" in interactive._failure_hint([build_item_runlog_entry(item, result)])


@pytest.mark.parametrize("title,body", [
    ("Just a moment...", "A browser extension panel without source content"),
    ("Attention Required! | Cloudflare", "Access denied"),
    ("Checking your browser...", "Please wait"),
    (None, "# Verification successful\n\nWaiting for the site to respond"),
    (None, "Verification successful. Waiting for linux.do to respond"),
    (None, "# Checking your browser before accessing example.test"),
])
def test_challenge_titles_or_leading_screens_fail_without_target_content(title, body):
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(replace(article(body), title=title), item)
    assert result.status == "failed"
    assert result.error_code == "browser_challenge_page"
    assert result.error_summary == "被站点验证页拦截，未取得目标正文。"
    assert not result.has_usable_text


def test_article_discussing_challenge_phrases_and_code_is_preserved():
    body = '# Understanding website access errors\n\nAn article about “Checking your browser” and “Attention Required”.\n\n```text\nVerification successful\n```\n\nOriginal explanation.'
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test")
    result = validate_extraction_content(replace(article(body), title="Website access troubleshooting"), item)
    assert result.status == "verified" and result.text == body


def test_head_unsupported_retries_get_and_reports_not_found(monkeypatch):
    monkeypatch.setattr(input_validation.socket, "getaddrinfo", lambda *a, **k: [])
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            method = request.get_method()
            calls.append(method)
            raise urllib.error.HTTPError(request.full_url, 405 if method == "HEAD" else 404, "fixture", {}, None)
    monkeypatch.setattr(input_validation.urllib.request, "build_opener", lambda *a: Opener())
    assert input_validation.network_preflight("https://example.test/missing") == "页面不存在（404）"
    assert calls == ["HEAD", "GET"]


@pytest.mark.parametrize("argv", [["这不是链接"], ["go", "这不是链接"]])
def test_invalid_input_short_circuits_before_network_or_extraction(argv, monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    monkeypatch.setattr(input_validation, "network_preflight", lambda url: pytest.fail("network on invalid input"))
    started = time.monotonic()
    assert main(argv) == 2
    assert time.monotonic() - started < 1
    assert capsys.readouterr().err.strip() == input_validation.INPUT_HINT


def test_noninteractive_only_needs_a_destination(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    assert main(["go", "https://example.test/article"]) == 2
    output = capsys.readouterr().err
    assert "-o" in output and "--save-to" in output
    for value in ("--yes", "--enable-network-backends", "--enable-browser-backends", "doctor"):
        assert value not in output


def test_skeleton_failure_is_not_tool_missing():
    assert "尚未实现" in interactive._failure_hint([{"error_code": "backend_not_implemented"}])
    assert "doctor" not in interactive._failure_hint([{"error_code": "backend_not_implemented"}])


def test_dns_failure_is_definitive(monkeypatch):
    monkeypatch.setattr(input_validation.urllib.request, "getproxies", lambda: {})
    def no_dns(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "no host")
    monkeypatch.setattr(input_validation.socket, "getaddrinfo", no_dns)
    assert input_validation.network_preflight("https://missing.test/nothing") == "域名无法访问"


@pytest.mark.parametrize("proxy_settings,expected", [
    ({"http": "http://proxy.test:8080", "https": "http://proxy.test:8443"}, {"http": "http://proxy.test:8080", "https": "http://proxy.test:8443"}),
    ({"all": "http://proxy.test:8080", "no": "localhost"}, {"all": "http://proxy.test:8080", "no": "localhost", "http": "http://proxy.test:8080", "https": "http://proxy.test:8080"}),
    ({"all": "http://proxy.test:8080", "https": "http://other.test:8080"}, {"all": "http://proxy.test:8080", "https": "http://other.test:8080", "http": "http://proxy.test:8080"}),
])
def test_preflight_uses_environment_proxy_and_does_not_require_local_origin_dns(monkeypatch, proxy_settings, expected):
    monkeypatch.setattr(input_validation.urllib.request, "getproxies", lambda: dict(proxy_settings))
    monkeypatch.setattr(input_validation.socket, "getaddrinfo", lambda *a, **k: pytest.fail("origin DNS before proxy"))
    captured = []
    real_handler = input_validation.urllib.request.ProxyHandler
    def handler(proxies):
        captured.append(dict(proxies))
        return real_handler(proxies)
    class Opener:
        def open(self, request, **kwargs):
            raise urllib.error.HTTPError(request.full_url, 404, "fixture", {}, None)
    monkeypatch.setattr(input_validation.urllib.request, "ProxyHandler", handler)
    monkeypatch.setattr(input_validation.urllib.request, "build_opener", lambda *a: Opener())
    assert input_validation.network_preflight("https://missing.test/nothing") == "页面不存在（404）"
    assert captured == [expected]


def test_proxy_timeout_remains_inconclusive(monkeypatch):
    monkeypatch.setattr(input_validation.urllib.request, "getproxies", lambda: {"https": "http://proxy.test:8080"})
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.get_method())
            raise urllib.error.URLError(TimeoutError("fixture"))
    monkeypatch.setattr(input_validation.urllib.request, "build_opener", lambda *a: Opener())
    assert input_validation.network_preflight("https://missing.test/nothing") is None
    # A transport timeout already spent the budget; do not stack a second wait.
    assert calls == ["HEAD"]


def test_reserved_invalid_domain_does_not_trust_fake_dns(monkeypatch):
    monkeypatch.setattr(input_validation.socket, "getaddrinfo", lambda *a, **k: pytest.fail("reserved domain queried"))
    assert input_validation.network_preflight("https://example.invalid/nothing") == "域名无法访问"


@pytest.mark.parametrize("code,expected", [(404, "页面不存在（404）"), (410, "页面不存在（410）"), (403, None), (429, None), (500, None)])
def test_http_head_falls_back_to_get_and_only_definitive_errors_block(monkeypatch, code, expected):
    monkeypatch.setattr(input_validation.socket, "getaddrinfo", lambda *a, **k: [])
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.get_method())
            raise urllib.error.HTTPError(request.full_url, code, "fixture", {}, None)
    monkeypatch.setattr(input_validation.urllib.request, "build_opener", lambda *a: Opener())
    assert input_validation.network_preflight("https://example.test/missing") == expected
    assert calls == (["HEAD"] if expected else ["HEAD", "GET"])


def test_timeout_does_not_block(monkeypatch):
    monkeypatch.setattr(input_validation, "_probe_url", lambda url: time.sleep(0.1))
    started = time.monotonic()
    assert input_validation.network_preflight("https://example.test", timeout=0.01) is None
    assert time.monotonic() - started < 0.1


def test_destination_enables_daily_gates_but_never_cloud(monkeypatch):
    from openmd.cli import build_parser
    args = build_parser().parse_args(["go", "https://example.test/article", "-o", "x"])
    assert interactive._authorize(args, args.url, tty=False)
    assert args.enable_network_backends and args.enable_browser_backends
    assert not getattr(args, "enable_cloud_backends", False)


def test_explicit_gate_flag_narrows_the_grant(monkeypatch):
    from openmd.cli import build_parser
    args = build_parser().parse_args(["go", "https://example.test/article", "-o", "x", "--enable-network-backends"])
    assert interactive._authorize(args, args.url, tty=False)
    assert args.enable_network_backends and not args.enable_browser_backends


def test_existing_file_is_accepted(tmp_path):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"fixture")
    assert input_validation.valid_daily_input(str(path))
    assert not input_validation.valid_daily_input(str(tmp_path / "missing.pdf"))


SOFT_404_BODY = """![](https://s.example.test/img-32.gif)
![](https://s.example.test/trans_1x1.gif)
![](https://s.example.test/bel-8.gif)

![](https://img.example.test/spacer.gif) **New:** [Making Startups Powerful](https://example.test/powerful.html) | [Universities](https://example.test/prep.html)
![](https://img.example.test/spacer.gif) **Want to start a startup?** Get funded by [Y Combinator](http://example.test/apply.html).

© mmxxvi pg
"""


def test_navigation_shell_soft_404_is_rejected():
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test/missing.html")
    result = validate_extraction_content(article(SOFT_404_BODY), item)
    assert result.status == "failed" and result.error_code == "content_navigation_shell" and result.text == ""


@pytest.mark.parametrize("body", [
    "# 短讯\n\n今天发布了新版本，修复了保存目录在 Windows 下偶发失败的问题，欢迎升级后反馈。",
    "A short note.\n\nSee [one](https://a.test), [two](https://b.test) and [three](https://c.test) for the background; this paragraph explains why each source matters to the argument.",
])
def test_short_genuine_posts_are_not_navigation_shells(body):
    item = ContentItem(platform="web_article", input_type="single", url="https://example.test/post")
    assert validate_extraction_content(article(body), item).status == "verified"


def test_cjk_body_after_source_url_and_links_is_not_a_shell():
    body = (
        "> 原文链接: https://mp.example.test/s/abc\n\n---\n\n"
        "[那天我写的文章](https://mp.example.test/s?a=1)\n\n[另一篇](https://mp.example.test/s?a=2)\n\n"
        "[第三篇](https://mp.example.test/s?a=3)\n\n这个问题的答案，不是统一的。要分析这件事，我给你们讲一个故事，讲一个面试中的场景。\n"
        "下面的对话源自昔日的面试，被我重新加工组织改造，便于读者理解。"
    )
    assert content_validation.navigation_shell_reason(body) is None


def test_navigation_shell_failure_hint_is_specific():
    hint = interactive._failure_hint([{"error_code": "content_navigation_shell"}])
    assert "导航" in hint and "doctor" not in hint
