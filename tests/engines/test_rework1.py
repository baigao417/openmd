from pathlib import Path
import pytest

from openmd.backends.executor import BackendExecutor
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.content_validation import html_tag_residue
from openmd.core.output import write_item_result
from openmd.core.package_writer import write_item_package
from openmd.core.renderer import render_document, render_markdown_document
from openmd.engines.base import success
from openmd.engines.tools import PandocEngine
from test_signatures import _container


@pytest.mark.parametrize("suffix", ["html", "htm", "epub", "rtf", "odt", "odp", "ods", "doc", "xls", "ppt", "csv", "tsv", "json", "xml", "ipynb", "msg", "mobi", "azw3", "fb2", "zip"])
@pytest.mark.parametrize("available", [True, False])
def test_new_formats_only_execute_document_provider(tmp_path, monkeypatch, suffix, available):
    from openmd.backends.registry import create_runtime_backend_provider_registry, BackendRuntimeOptions
    from openmd.adapters.registry import create_default_registry
    from openmd.engines.base import failure

    source = tmp_path / ("source." + suffix)
    source.write_bytes(b"fixture")
    item = ContentItem(platform="pdf", input_type="single", url=source.as_uri(), source_type="document", raw_meta={"local_path": str(source)})
    monkeypatch.setattr("openmd.engines.registry.convert_document", lambda path: success("document.engines", path, "Original source text.") if available else failure("document.engines", path, "engine_unavailable"))
    providers = create_runtime_backend_provider_registry("pdf", BackendRuntimeOptions())
    def unexpected(request):
        pytest.fail("Inapplicable legacy PDF/OCR provider executed")
    for name in ("pdf.markitdown", "pdf.textlayer", "ocr.chain"):
        monkeypatch.setattr(providers.provider_for(name), "extract", unexpected)
    result = BackendExecutor(provider_registry=providers).extract(item, create_default_registry().backend_chain("pdf"))
    assert result.status == ("verified" if available else "failed")
    assert "backend_chain: document.engines" in result.source_notes
    assert all(attempt["backend"] == "document.engines" for attempt in result.metadata.get("fallback_attempts", []))
    assert not any("pdf_local_file_required" in note for note in result.source_notes)


@pytest.mark.parametrize("suffix,engine", [("doc", "soffice"), ("xls", "soffice"), ("ppt", "soffice"), ("odp", "soffice"), ("ods", "soffice"), ("mobi", "calibre"), ("azw3", "calibre"), ("fb2", "calibre")])
@pytest.mark.parametrize("available", [True, False])
def test_plan_checks_external_document_dependency_without_reading_source(tmp_path, monkeypatch, suffix, engine, available):
    from openmd.core.planner import build_plan
    source = tmp_path / ("source." + suffix)
    source.write_bytes(b"fixture")
    kind = "SofficeEngine" if engine == "soffice" else "CalibreEngine"
    monkeypatch.setattr('openmd.engines.native.NativeEngine.available', lambda self: {'ok': False, 'reason': 'test missing native', 'version': None})
    monkeypatch.setattr("openmd.engines.tools." + kind + ".available", lambda self: {"ok": available, "reason": "test", "version": "test"})
    original_open = Path.open
    def guard(path, *args, **kwargs):
        assert path != source, "plan must not read the input body"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guard)
    result = build_plan(str(source), available_gates={"local_compute"})
    assert result.execution_ready is available
    assert result.runtime_provider == "document.engines"
    assert result.blocked_reason == (None if available else "engine_unavailable:" + engine)
    if not available:
        assert result.as_dict()["next_action"]


def test_non_document_providers_are_not_recorded_as_failed_attempts(tmp_path):
    source = tmp_path / "source.html"
    source.write_text("<html><body><h1>Title</h1><p>Body</p></body></html>", encoding="utf-8")
    item = ContentItem(platform="pdf", input_type="single", url=source.as_uri(), source_type="document", raw_meta={"local_path": str(source)})

    class NotApplicable:
        name = "pdf.markitdown"
        def extract(self, request):
            return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=request.primary_backend, error_code="backend_not_applicable")

    class Document:
        name = "document.engines"
        def extract(self, request):
            return success("defuddle", source, "# Title\n\nBody", source_format="html")

    result = BackendExecutor(providers={"pdf.markitdown": NotApplicable(), "document.engines": Document()}).extract(
        item, ("pdf.markitdown", "document.engines")
    )
    assert result.status == "verified"
    assert "fallback_attempts" not in result.metadata
    assert not any("pdf.markitdown failed" in note for note in result.source_notes)


def test_no_fallback_still_selects_the_applicable_document_provider(tmp_path):
    source = tmp_path / "source.html"
    source.write_text("<p>Source text</p>", encoding="utf-8")
    item = ContentItem(platform="pdf", input_type="single", url=source.as_uri(), source_type="document")
    class Document:
        def extract(self, request):
            return success("document.engines", source, "Source text")
    result = BackendExecutor(providers={"document.engines": Document()}).extract(
        item, ("pdf.markitdown", "pdf.textlayer", "ocr.chain", "document.engines"), allow_fallback=False
    )
    assert result.status == "verified"
    assert "backend_chain: document.engines" in result.source_notes


def test_pandoc_output_has_no_raw_html_or_duplicate_title(tmp_path, monkeypatch):
    path = tmp_path / "source.epub"
    path.write_bytes(_container("application/epub+zip", "META-INF/container.xml"))
    monkeypatch.setattr("openmd.engines.tools.executable", lambda tool: "pandoc")
    monkeypatch.setattr(
        "openmd.engines.tools.run_command",
        lambda argv, directory, limits: "<span id=\"ch001.xhtml\"># source</span>\n\n<div class=\"section level1\"># Chapter one\n\nBody</div>",
    )
    result = PandocEngine().convert(path)
    assert result.status == "verified"
    assert html_tag_residue(result.text) == (0, 0)
    assert result.text.count("# source") == 0
    assert "Chapter one" in result.text


@pytest.mark.parametrize("suffix", ["html", "epub", "zip", "docx"])
def test_document_format_is_written_to_plain_and_package_outputs(tmp_path, suffix):
    source = tmp_path / ("source." + suffix)
    source.write_text("<html><body><h1>Title</h1><p>Body</p></body></html>", encoding="utf-8")
    item = ContentItem(platform="pdf", input_type="single", url=source.as_uri(), title="Title", source_type="document", raw_meta={"local_path": str(source)})
    result = success("defuddle", source, "# Title\n\nBody", source_format=suffix)
    document = render_markdown_document(item, result)
    text = render_document(document)
    assert "format: " + suffix in text
    assert write_item_result(item, result, tmp_path / "plain").read_text(encoding="utf-8").find("format: " + suffix) >= 0
    assert "## Source\n\n- Format: " + suffix in text
    package = write_item_package(item, result, tmp_path / "packages", skip_existing=False).package_path
    index = (package / "index.md").read_text(encoding="utf-8")
    assert "format: " + suffix in index
    assert "- Format: " + suffix in index
