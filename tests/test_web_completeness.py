from __future__ import annotations

from openmd.backends.base import BackendExtractRequest
from openmd.backends.snapshot import SnapshotProvider
from openmd.backends.web_article import apply_completeness_guard
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
