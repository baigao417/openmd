from __future__ import annotations

from openmd.backends.base import BackendExtractRequest
from openmd.backends.snapshot import SnapshotProvider
from openmd.backends.executor import BackendExecutor
from openmd.backends.web_article import (
    _arxiv_html_url,
    _fetch_arxiv_html,
    _format_rfc_plain_text,
    apply_completeness_guard,
)
from openmd.core.models import ContentItem, TranscriptResult


def _result(backend: str, text: str) -> TranscriptResult:
    return TranscriptResult(
        text=text,
        status="verified",
        source_level="native_text",
        backend=backend,
    )


def test_completeness_guard_selects_longer_non_shell_candidate() -> None:
    short = _result("defuddle.web.content", "# RFC\n\n6.4 short fragment")
    long = _result("web_article.trafilatura", "# RFC\n\n" + ("6.4 complete body. " * 30))

    selected = apply_completeness_guard(
        [short, long],
        html="<html><body><article>" + ("6.4 complete body. " * 60) + "</article></body></html>",
        title="RFC",
    )

    assert selected.backend == "web_article.trafilatura"
    assert selected.metadata["extraction_candidates"][1]["selected"] is True
    assert selected.metadata["coverage_estimate"]["selected_chars"] > 100


def test_frozen_arxiv_abstract_keeps_complete_defuddle_primary_candidate() -> None:
    # Frozen arXiv abstract-page structure with synthetic prose. The real page
    # includes an information table (Comments, Subjects, Cite as, DOI) that
    # Trafilatura omits; keep that structure without copying the paper text.
    html = """<!doctype html><html><body>
      <header>arXiv navigation and search</header>
      <main><article id="abs">
        <h1>Attention Is All You Need</h1>
        <blockquote class="abstract">We introduce a model based entirely on attention.
          It replaces recurrence with parallel processing and improves translation results.
          Experiments confirm strong quality across two translation tasks.</blockquote>
        <table class="metadata"><tr><th>Comments</th><td>15 pages, 5 figures</td></tr>
          <tr><th>Subjects</th><td>Computation and Language</td></tr>
          <tr><th>Cite as</th><td>arXiv:1706.03762</td></tr>
          <tr><th>DOI</th><td>10.48550/arXiv.1706.03762</td></tr></table>
      </article></main></body></html>"""
    defuddle = _result(
        "defuddle.web.content",
        "# Attention Is All You Need\n\nAbstract: We introduce a model based entirely on attention. "
        "It replaces recurrence with parallel processing and improves translation results. "
        "Experiments confirm strong quality across two translation tasks.\n\n"
        "Comments: 15 pages, 5 figures\nSubjects: Computation and Language\n"
        "Cite as: arXiv:1706.03762\nDOI: 10.48550/arXiv.1706.03762",
    )
    trafilatura = _result(
        "web_article.trafilatura",
        "# Attention Is All You Need\n\n" + ("Abstract prose expanded by the alternate extractor. " * 9),
    )

    selected = apply_completeness_guard([defuddle, trafilatura], html=html)

    assert selected.backend == "defuddle.web.content"
    assert "Subjects: Computation and Language" in selected.text
    assert selected.metadata["extraction_candidates"][0]["selected"] is True


def test_completeness_guard_does_not_prefer_longer_navigation_tail() -> None:
    complete = _result(
        "defuddle.web.content",
        "# Article\n\nA substantial abstract explains the method and results in clear prose.\n\n"
        "The model improves translation quality with attention and parallel computation.",
    )
    noisy = _result(
        "web_article.trafilatura",
        complete.text + "\n\n## More from this page\n\n"
        + "\n".join(f"[Related resource {index}](https://example.test/{index})" for index in range(12)),
    )

    selected = apply_completeness_guard(
        [complete, noisy],
        html="<html><body><article>" + complete.text + "</article><nav>" + ("link " * 200) + "</nav></body></html>",
    )

    assert selected.backend == "defuddle.web.content"
    records = selected.metadata["extraction_candidates"]
    assert records[0]["selected"] is True
    assert records[1]["noise_chars"] > records[0]["noise_chars"]
    assert selected.metadata["completeness"]["extraction_candidates"] == records
    assert selected.metadata["completeness"]["coverage_estimate"] == selected.metadata["coverage_estimate"]


def test_completeness_guard_discount_link_heavy_feature_modules() -> None:
    complete = _result("defuddle.web.content", "# Paper\n\nAbstract content with enough prose to represent the article body clearly.")
    noisy = _result(
        "web_article.trafilatura",
        complete.text + "\n\n### Additional Features\n\n"
        + "\n\n".join(f"[{name}](https://example.test/{index})" for index, name in enumerate(("Bookmark", "Share", "Cite", "Download", "Explore", "Follow"))),
    )

    selected = apply_completeness_guard([complete, noisy], body_chars=240)

    assert selected.backend == "defuddle.web.content"
    assert selected.metadata["extraction_candidates"][1]["noise_chars"] > 0


def test_completeness_guard_marks_small_coverage_partial() -> None:
    result = _result("defuddle.web.content", "# RFC\n\n6.4 short fragment")

    selected = apply_completeness_guard(
        [result],
        html="<html><body><article>" + ("long source paragraph. " * 100) + "</article></body></html>",
        title="RFC",
    )

    assert selected.status == "partial"
    assert selected.error_code == "partial_coverage"
    assert "可能只截取了部分正文" in " ".join(selected.source_notes)


def test_snapshot_provider_applies_same_completeness_guard(tmp_path) -> None:
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><article><h1>Article</h1>" + ("complete paragraph. " * 40) + "</article></body></html>",
        encoding="utf-8",
    )

    provider = SnapshotProvider(runner=lambda _path, _html: {"contentMarkdown": "# Article\n\nshort"})
    item = ContentItem(
        platform="page_snapshot",
        input_type="single",
        source_type="article",
        url="https://example.test/article",
        raw_meta={"snapshot_path": str(path)},
    )

    result = provider.extract(BackendExtractRequest(item, (provider.name,)))

    assert result.status == "partial"
    assert result.metadata["coverage_estimate"]["body_chars"] > result.metadata["coverage_estimate"]["selected_chars"]


def test_web_candidates_share_one_fetched_html_and_preserve_route_failures(monkeypatch) -> None:
    import openmd.backends.web_article as web_article_module

    html = "<html><body><article>Stable article source body with enough text.</article></body></html>"
    fetches = []
    monkeypatch.setattr(web_article_module, "_fetch_web_html", lambda url: (fetches.append(url) or (html, {"route": "direct", "attempts": 2, "http_status": 200, "retry_history": [{"route": "proxy", "outcome": "web_fetch_timeout", "http_status": None}, {"route": "direct", "outcome": "success", "http_status": 200}]})))

    class Candidate:
        def __init__(self, name, code, status):
            self.name, self.code, self.http_status = name, code, status

        def extract(self, request):
            assert request.item.raw_meta["source_html"] == html
            assert request.item.raw_meta["source_fetch"]["http_status"] == 200
            return TranscriptResult(
                text="", status="failed", source_level="partial_extraction", backend=self.name,
                error_code=self.code, metadata={"http_status": self.http_status},
            )

    def provider_result(name, code, http_status):
        return Candidate(name, code, http_status)

    providers = {
        "defuddle.web.content": provider_result("defuddle.web.content", "web_fetch_failed", 502),
        "web_article.trafilatura": provider_result("web_article.trafilatura", "web_fetch_failed", 504),
    }
    item = ContentItem(platform="web_article", input_type="single", source_type="article", url="https://example.test/article")
    result = BackendExecutor(providers=providers).extract(item, tuple(providers))

    assert fetches == [item.url]
    assert result.status == "failed"
    assert result.metadata["web_route_diagnostics"] == [
        {"backend": "defuddle.web.content", "error_code": "web_fetch_failed", "http_status": 502},
        {"backend": "web_article.trafilatura", "error_code": "web_fetch_failed", "http_status": 504},
    ]
    assert result.metadata["fallback_attempts"] == result.metadata["web_route_diagnostics"]
    assert any("web_fetch_attempt: proxy web_fetch_timeout" in note for note in result.source_notes)
    assert all(item.url not in note for note in result.source_notes)


def test_web_fetch_retries_proxy_then_direct_without_persisting_url(monkeypatch) -> None:
    import urllib.error
    import openmd.backends.web_article as web_article_module

    routes = []
    class Opener:
        def __init__(self, route):
            self.route = route
        def open(self, request, timeout):
            routes.append(self.route)
            if self.route == "proxy":
                raise urllib.error.URLError(TimeoutError("connect"))
            class Headers:
                def get_content_charset(self):
                    return "utf-8"
            class Response:
                status = 200
                headers = Headers()
                def __enter__(self): return self
                def __exit__(self, *_args): return None
                def read(self, _limit): return b"<html><article>Fetched once</article></html>"
            return Response()

    monkeypatch.setattr(web_article_module.urllib.request, "build_opener", lambda *handlers: Opener("direct" if handlers else "proxy"))
    html, receipt = web_article_module._fetch_web_html("https://example.test/path?signature=redacted")

    assert routes == ["proxy", "direct"]
    assert html == "<html><article>Fetched once</article></html>"
    assert receipt["http_status"] == 200
    assert all("signature" not in str(attempt) for attempt in receipt["retry_history"])


def test_web_rate_limit_page_is_rejected_before_extractors_run(monkeypatch) -> None:
    import openmd.backends.web_article as web_article_module

    html = "<html><head><title>Too Many Requests</title></head><body>Please retry later.</body></html>"
    monkeypatch.setattr(web_article_module, "_fetch_web_html", lambda _url: (html, {"route": "proxy", "attempts": 1, "http_status": 200}))

    class MustNotRun:
        def extract(self, _request):
            raise AssertionError("error pages must not reach content extractors")

    item = ContentItem(platform="web_article", input_type="single", source_type="article", url="https://example.test/article")
    result = BackendExecutor(providers={"defuddle.web.content": MustNotRun(), "web_article.trafilatura": MustNotRun()}).extract(
        item, ("defuddle.web.content", "web_article.trafilatura")
    )

    assert result.status == "failed"
    assert result.error_code == "web_error_page"
    assert [row["error_code"] for row in result.metadata["web_route_diagnostics"]] == ["web_error_page", "web_error_page"]
    assert "Please retry later" not in str(result.metadata)


def test_rfc_plain_text_conversion_preserves_full_sections_and_abnf_blocks() -> None:
    from openmd.backends.web_article import _format_rfc_plain_text

    source = (
        "RFC 9110                 HTTP Semantics                 June 2022\n"
        "\n"
        "1. Introduction\n"
        "This document defines HTTP semantics and message handling.\n"
        "\n"
        "1.1. Purpose\n"
        "The protocol provides a uniform interface.\n"
        "\n"
        "field-name = token\n"
        "token = 1*tchar\n"
        "\n"
        "16. Security Considerations\n"
        "Implementations must consider intermediary attacks.\n"
        "\n"
        "GET /hello.txt HTTP/1.1\n"
        "Host: www.example.com\n"
        "\n"
        "Appendix A. Collected ABNF\n"
        "message = start-line *( field-line CRLF ) CRLF\n"
        "\n"
        "RFC 9110                 HTTP Semantics                 June 2022\n"
        "[Page 1]\n"
    )

    markdown = _format_rfc_plain_text(source)

    assert markdown.startswith("# RFC 9110")
    assert "## 1. Introduction" in markdown
    assert "### 1.1. Purpose" in markdown
    assert "## 16. Security Considerations" in markdown
    assert "## Appendix A. Collected ABNF" in markdown
    assert markdown.count("```") >= 4
    assert "field-name = token" in markdown and "message = start-line" in markdown
    assert "[Page 1]" not in markdown


def test_arxiv_abs_uses_html_fulltext_and_falls_back_to_partial_abstract(monkeypatch) -> None:
    import openmd.backends.web_article as web_article_module

    assert _arxiv_html_url("https://arxiv.org/abs/1706.03762") == "https://arxiv.org/html/1706.03762"
    assert _arxiv_html_url("https://arxiv.org/abs/1706.03762v7") == "https://arxiv.org/html/1706.03762v7"
    assert _arxiv_html_url("https://example.test/abs/1706.03762") is None

    calls = []

    def fulltext_available(url):
        calls.append(url)
        if "/abs/" in url:
            return ("<html><head><meta name='citation_title' content='Sample Paper'>"
                    "<meta name='citation_author' content='A. Author'>"
                    "<meta name='citation_date' content='2024/01/02'></head><body>abstract</body></html>",
                    {"http_status": 200, "route": "proxy", "retry_history": []})
        return ("<html><article>full paper</article></html>", {"http_status": 200, "route": "proxy", "retry_history": []})

    monkeypatch.setattr(web_article_module, "_fetch_web_html", fulltext_available)
    fulltext, receipt = _fetch_arxiv_html("https://arxiv.org/abs/1706.03762")
    assert calls == ["https://arxiv.org/abs/1706.03762", "https://arxiv.org/html/1706.03762"]
    assert "full paper" in fulltext
    assert receipt["arxiv_full_text"]["available"] is True
    assert receipt["arxiv_metadata"] == {
        "title": "Sample Paper", "authors": ["A. Author"], "published_at": "2024/01/02"
    }

    calls.clear()

    def fulltext_missing(url):
        calls.append(url)
        if "/html/" in url:
            raise web_article_module.WebFetchError("web_fetch_http_error", http_status=404, attempts=[])
        return ("<html><article>abstract only</article></html>", {"http_status": 200, "route": "proxy", "retry_history": []})

    monkeypatch.setattr(web_article_module, "_fetch_web_html", fulltext_missing)
    abstract, receipt = _fetch_arxiv_html("https://arxiv.org/abs/1706.03762")
    assert calls == ["https://arxiv.org/abs/1706.03762", "https://arxiv.org/html/1706.03762"]
    assert "abstract only" in abstract
    assert receipt["arxiv_full_text"]["available"] is False
    assert receipt["arxiv_full_text"]["error_code"] == "web_fetch_http_error"


def test_executor_uses_rfc_editor_plain_text_instead_of_short_html_extractors(monkeypatch) -> None:
    import openmd.backends.web_article as web_article_module

    source = (
        "RFC 9110                 HTTP Semantics                 June 2022\n\n"
        "1. Introduction\nComplete RFC body for this test.\n\n"
        "16. Security Considerations\nFinal section content.\n\n"
        "Appendix A. Collected ABNF\nfield-name = token\n"
    )
    monkeypatch.setattr(
        web_article_module,
        "_fetch_web_html",
        lambda url: (source, {"http_status": 200, "route": "proxy", "retry_history": []}),
    )

    class MustNotRun:
        def extract(self, _request):
            raise AssertionError("complete RFC plain text should bypass short article extractors")

    item = ContentItem(
        platform="web_article",
        input_type="single",
        source_type="article",
        url="https://www.rfc-editor.org/rfc/rfc9110",
    )
    result = BackendExecutor(providers={
        "defuddle.web.content": MustNotRun(),
        "web_article.trafilatura": MustNotRun(),
    }).extract(item, ("defuddle.web.content", "web_article.trafilatura"))

    assert result.backend == "rfc_editor.plain_text"
    assert result.status == "verified"
    assert "## 16. Security Considerations" in result.text
    assert "field-name = token" in result.text


def test_executor_marks_arxiv_abstract_partial_when_html_fulltext_is_missing(monkeypatch) -> None:
    import openmd.backends.web_article as web_article_module

    calls = []

    def fetch(url):
        calls.append(url)
        if "/html/" in url:
            raise web_article_module.WebFetchError("web_fetch_http_error", http_status=404, attempts=[])
        return ("<html><body><article><h1>Paper</h1><p>Abstract summary.</p></article></body></html>",
                {"http_status": 200, "route": "proxy", "retry_history": []})

    monkeypatch.setattr(web_article_module, "_fetch_web_html", fetch)

    class AbstractExtractor:
        def __init__(self, name):
            self.name = name

        def extract(self, request):
            assert "Abstract summary" in request.item.raw_meta["source_html"]
            return TranscriptResult(
                text="# Paper\n\nAbstract summary.", status="verified", source_level="native_text",
                backend=self.name,
            )

    item = ContentItem(
        platform="web_article",
        input_type="single",
        source_type="article",
        url="https://arxiv.org/abs/1706.03762",
    )
    result = BackendExecutor(providers={
        "defuddle.web.content": AbstractExtractor("defuddle.web.content"),
        "web_article.trafilatura": AbstractExtractor("web_article.trafilatura"),
    }).extract(item, ("defuddle.web.content", "web_article.trafilatura"))

    assert calls == ["https://arxiv.org/abs/1706.03762", "https://arxiv.org/html/1706.03762"]
    assert result.status == "partial"
    assert result.error_code == "arxiv_full_text_unavailable"
    assert "Abstract summary" in result.text
