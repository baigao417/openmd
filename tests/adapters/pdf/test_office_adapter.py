from pathlib import Path
from unittest.mock import patch

import pytest

from openmd.adapters.pdf.adapter import PdfDocumentAdapter
from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
from openmd.adapters.pdf.backends.markitdown import _has_document_text
from openmd.adapters.registry import create_default_registry
from openmd.core.detector import detect_platform
from openmd.core.planner import build_plan


@pytest.mark.parametrize("suffix", [".docx", ".pptx", ".xlsx", ".DOCX"])
def test_office_routes_to_existing_local_document_backend(tmp_path, suffix):
    path = tmp_path / f"source{suffix}"
    path.write_bytes(b"fixture")
    for value in (str(path), path.as_uri()):
        assert detect_platform(value) == "pdf"
        adapter = create_default_registry().adapter_for(value)
        assert isinstance(adapter, PdfDocumentAdapter)
        assert adapter.can_handle(value)
        item = adapter.resolve_items(value)[0]
        assert item.source_type == "document"
        assert item.raw_meta["permission_scope"] == "local_file"
        original_open = Path.open

        def guard_input_open(open_path, *args, **kwargs):
            assert open_path.resolve() != path.resolve(), "plan read input body"
            return original_open(open_path, *args, **kwargs)

        with patch.object(Path, "open", guard_input_open):
            plan = build_plan(value, available_gates={"local_compute"})
        assert plan.execution_ready
        from openmd.engines.native import PROMOTED
        assert plan.runtime_provider == ("document.engines" if suffix.lower().lstrip('.') in PROMOTED else "pdf.markitdown")


def test_document_folder_includes_office_files_but_not_extension_named_directory(tmp_path):
    for suffix in (".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".doc"):
        (tmp_path / f"source{suffix}").write_text(suffix)
    (tmp_path / "not-a-file.docx").mkdir()
    items = PdfDocumentAdapter().resolve_items(str(tmp_path))
    assert {Path(item.raw_meta["local_path"]).suffix for item in items} == {".pdf", ".docx", ".pptx", ".xlsx", ".doc"}
    assert len(items) == 5


@pytest.mark.parametrize("suffix", [".docx", ".pptx", ".xlsx"])
def test_remote_office_is_rejected_before_parser_import(suffix):
    with patch.object(MarkItDownPDFParser, "is_available", side_effect=AssertionError("dependency probe")):
        result = MarkItDownPDFParser().parse(f"https://example.com/source{suffix}", title="source")
    assert result.status == "failed"
    assert result.error_code == "document_local_file_required"


def test_legacy_doc_is_not_advertised_as_executable(tmp_path, monkeypatch):
    monkeypatch.setattr("openmd.engines.tools.SofficeEngine.available", lambda self: {"ok": False, "reason": "missing", "version": None})
    path = tmp_path / "source.doc"
    path.write_bytes(b"fixture")
    plan = build_plan(str(path), available_gates={"local_compute"})
    assert not plan.execution_ready
    assert plan.blocked_reason == "engine_unavailable:soffice"
    assert plan.next_action


def test_legacy_doc_plan_is_ready_when_soffice_is_available(tmp_path, monkeypatch):
    path = tmp_path / "source.doc"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "openmd.engines.tools.SofficeEngine.available",
        lambda self: {"ok": True, "reason": "test", "version": "test"},
    )
    plan = build_plan(str(path), available_gates={"local_compute"})
    assert plan.execution_ready
    assert plan.blocked_reason is None


@pytest.mark.parametrize("suffix", [".docx", ".pptx", ".xlsx"])
def test_office_plain_text_cannot_be_verified_by_converter_fallback(tmp_path, suffix):
    path = tmp_path / f"corrupt{suffix}"
    path.write_text("plain text is not a valid Office document")
    with patch.object(MarkItDownPDFParser, "is_available", side_effect=AssertionError("parser must not run")):
        result = MarkItDownPDFParser().parse(str(path), title="corrupt")
    assert result.status == "failed"
    assert result.error_code == "document_invalid_container"


@pytest.mark.parametrize("text,source_format,expected", [
    ("<!-- Slide number: 1 -->", "pptx", False),
    ("## Sheet\n|\n|  |", "xlsx", False),
    ("## Sheet\n| Unnamed: 0 |\n| --- |", "xlsx", False),
    ("## Sheet\n| Item | Value |\n| --- | --- |\n| Apple | 0 |", "xlsx", True),
    ("<!-- Slide number: 1 -->\n# Real slide title", "pptx", True),
])
def test_document_structure_alone_is_not_native_text(text, source_format, expected):
    assert _has_document_text(text, source_format) is expected
