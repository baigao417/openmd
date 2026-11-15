"""Keep MarkItDown Office defaults; recover suspected visual/text loss locally."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openmd.core.local_config import load_local_config
from openmd.runtime.interpreters import owned_popen, process_options, terminate_quietly, wait_interruptibly
from .config import settings

from openmd.runtime.tempdirs import owned_temporary_directory


_WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_REL_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
# An exemption proves text-only use, rather than trusting a relationship Type.
_TEXT_LINK_TAGS = {"hyperlink", "r", "t", "rPr", "rStyle", "b", "bCs", "i", "iCs",
                   "u", "color", "sz", "szCs", "rFonts", "lang", "highlight", "vertAlign"}
_EXTERNAL_FIELD = re.compile(r"^\s*(INCLUDEPICTURE|INCLUDETEXT|LINK)\b", re.I)
_FIELD_TARGET = re.compile(r'''(?:[a-z][a-z0-9+.-]*://|\\\\|//)[^\s"'<>]+''', re.I)


def _target_host(target: str) -> str | None:
    """Retain a destination hostname without userinfo, port, path or query."""
    try:
        parsed = urlsplit(target.strip().replace("\\", "/"))
        return parsed.hostname
    except ValueError:
        return None


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _attribute(node, name: str) -> str:
    prefix = node.tag.rsplit("}", 1)[0] + "}" if node.tag.startswith("{") else ""
    return node.get(prefix + name, node.get(name, ""))


def _field_instructions(root):
    """Read both simple and run-split Word fields without reading display text."""
    stack: list[list[str]] = []
    orphans: list[str] = []
    for node in root.iter():
        kind = _local_name(node.tag)
        if kind == "fldSimple":
            yield _attribute(node, "instr")
        elif kind == "fldChar":
            marker = _attribute(node, "fldCharType")
            if marker == "begin":
                stack.append([])
            elif marker in {"separate", "end"} and stack:
                yield "".join(stack[-1])
                if marker == "end":
                    stack.pop()
        elif kind in {"instrText", "delInstrText"}:
            text = "".join(node.itertext())
            if stack:
                # Nested QUOTE/IF fields can supply a parent's resource target.
                # Collect their instructions in every enclosing field as well.
                for pieces in stack:
                    pieces.append(text)
            else:
                orphans.append(text)
                yield text
    for pieces in stack:
        yield "".join(pieces)
    # Incomplete fields can still be acted upon by a repairing converter.
    yield "".join(orphans)


def _field_requires_resource(instruction: str, command_end: int) -> bool:
    """Only exempt an explicit local drive target; dynamic/unknown fields deny.

    In particular a single backslash may be a converter-repaired network path.
    Resolving Word field expressions is not a safe preflight operation.
    """
    target = instruction[command_end:].strip()
    # LINK has an application identifier before its filename. Treat it as an
    # external dependency whenever its target cannot be proved a local path.
    if instruction[:command_end].strip().upper() == "LINK":
        return True
    return not any(re.fullmatch(pattern, target) for pattern in (
        r'''"[a-zA-Z]:[\\/][^"'\r\n:{}]+"''',
        r"'[a-zA-Z]:[\\/][^\"'\r\n:{}]+'",
        r'''[a-zA-Z]:[\\/][^\s"':{}]+''',
    ))


def _text_hyperlink(node) -> bool:
    return bool("".join(node.itertext()).strip()) and all(
        child.tag in {"{" + namespace + "}" + tag for namespace in _WORD_NAMESPACES for tag in _TEXT_LINK_TAGS}
        for child in node.iter()
    )


def _only_text_hyperlink_references(root, relation_id: str) -> bool:
    if root is None or not relation_id:
        return False
    references = []
    for node in root.iter():
        for attribute, value in node.attrib.items():
            if relation_id not in value.split():
                continue
            references.append(
                node.tag in {"{" + namespace + "}hyperlink" for namespace in _WORD_NAMESPACES}
                and attribute in {"{" + namespace + "}id" for namespace in _REL_NAMESPACES}
                and value == relation_id
                and _text_hyperlink(node)
            )
    return bool(references) and all(references)


def _relationship_source(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.parent.name != "_rels" or path.name == ".rels":
        return None
    return str(path.parent.parent / path.name.removesuffix(".rels"))


def inspect_external_resources(path: Path) -> dict:
    """Preflight every relationship and Word field before launching LibreOffice.

    Relationship targets and field contents may contain credentials; receipts only
    contain relationship/field types and hostnames. Parsing never resolves URLs.
    Malformed containers/parts fail closed in the caller.
    """
    relationship_types: set[str] = set()
    field_types: set[str] = set()
    target_hosts: set[str] = set()
    inspected_parts = 0
    hyperlinks = 0
    with ZipFile(path) as archive:
        roots = {}
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name.lower().endswith((".rels", ".xml", ".vml")):
                continue
            if name in roots:
                raise ValueError("duplicate_office_xml_part")
            root = ElementTree.fromstring(archive.read(info))
            roots[name] = root
            inspected_parts += 1
        for name, root in list(roots.items()):
            if name.lower().endswith(".rels"):
                source_name = _relationship_source(name)
                source = roots.get(source_name)
                # OPC parts need not have an .xml extension. Parse the actual
                # referring source part before granting its exemption.
                if source is None and source_name in archive.namelist():
                    source = ElementTree.fromstring(archive.read(source_name))
                    inspected_parts += 1
                relation_ids = [node.get("Id", "") for node in root.iter() if _local_name(node.tag) == "Relationship"]
                for node in root.iter():
                    if _local_name(node.tag) != "Relationship" or not any(
                        _local_name(key) == "TargetMode" and value.strip().lower() == "external"
                        for key, value in node.attrib.items()
                    ):
                        continue
                    relation_type = node.get("Type", "")
                    relation_id = node.get("Id", "")
                    well_formed = (relation_id == relation_id.strip()
                                   and all(node.get(key, "").strip() for key in ("Id", "Type", "Target", "TargetMode"))
                                   and all(key in {"Id", "Type", "Target", "TargetMode"} for key in node.attrib))
                    if well_formed and relation_ids.count(relation_id) == 1 and _only_text_hyperlink_references(source, relation_id):
                        hyperlinks += 1
                    else:
                        relationship_types.add(relation_type or "unknown")
                        host = _target_host(node.get("Target", ""))
                        if host:
                            target_hosts.add(host)
            if name.lower().startswith("word/") and name.lower().endswith((".xml", ".vml")):
                for instruction in _field_instructions(root):
                    match = _EXTERNAL_FIELD.match(instruction)
                    if match and _field_requires_resource(instruction, match.end()):
                        field_types.add(match.group(1).upper())
                        for target in _FIELD_TARGET.findall(instruction[match.end():]):
                            host = _target_host(target)
                            if host:
                                target_hosts.add(host)
    return {
        "blocked": bool(relationship_types or field_types),
        "relationship_types": sorted(relationship_types),
        "field_types": sorted(field_types),
        "target_hosts": sorted(target_hosts),
        "hyperlinks": hyperlinks,
        "inspected_parts": inspected_parts,
    }


def soffice_path() -> str | None:
    explicit = os.getenv("OPENMD_SOFFICE") or load_local_config().get("soffice")
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else shutil.which(explicit)
    discovered = shutil.which("soffice") or shutil.which("soffice.exe")
    if discovered:
        return discovered
    for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)")):
        if root:
            path = Path(root) / "LibreOffice/program/soffice.exe"
            if path.is_file():
                return str(path)
    return None


def suspected_loss(path: Path, text: str, config: dict) -> dict:
    objects = {"images": 0, "charts": 0, "smartart": 0}
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            objects["images"] = sum("/media/" in name and not name.endswith("/") for name in names)
            objects["charts"] = sum(bool(re.search(r"/charts/chart\d+\.xml$", name)) for name in names)
            objects["smartart"] = sum(bool(re.search(r"/diagrams/data\d+\.xml$", name)) for name in names)
    except (OSError, BadZipFile):
        return {"suspected": False, "reason": "invalid_container", **objects}
    visible = re.sub(r"<!--.*?-->|<[^>]+>|!\[[^\]]*\]\([^)]*\)", "", text, flags=re.S)
    visible = re.sub(r"^#{1,6} .*?$", "", visible, flags=re.M)
    count = sum(c.isalnum() for c in visible)
    loss = count == 0 or (sum(objects.values()) > 0 and count < config["office_sparse_chars"])
    return {"suspected": loss, "reason": "empty_result" if count == 0 else "visual_objects_sparse_text" if loss else None, "text_chars": count, **objects}


def recover_office(item, result, *, allow_ocr: bool, allow_external_resources: bool = False):
    from .documents import extract_pdf

    path = Path(item.raw_meta["local_path"])
    detection = suspected_loss(path, result.text if result.has_usable_text else "", settings())
    if not detection["suspected"]:
        return replace(result, metadata={**result.metadata, "office_loss_check": detection})
    resources = None
    external_fetched = False

    def external_metadata():
        return {"office_external_resources_fetched": external_fetched,
                **({"external_resources": resources, "external_resources_allowed": allow_external_resources} if resources is not None else {})}

    def external_notes():
        return ["Office content depends on external resources; --enable-network-backends allowed LibreOffice to fetch them. Resource availability and content may change."] if external_fetched else []

    def incomplete(reason: str, extracted=None):
        recovery = {"status": "blocked" if reason == "office_external_resource_blocked" else "unavailable", "reason": reason}
        recovery.update(external_metadata())
        pages = {"pages": extracted.metadata["pages"]} if extracted is not None and "pages" in extracted.metadata else {}
        return replace(result, status="partial", error_code=reason, error_summary=reason, source_notes=[*result.source_notes, *external_notes(), "Office content may be missing: " + detection["reason"], "Office PDF recovery unavailable: " + reason + "; usable native text was retained. No automatic installation was attempted."], metadata={**result.metadata, **pages, "office_loss_check": detection, "office_external_resources_fetched": external_fetched, "office_recovery": recovery, "terminal_partial": True})

    try:
        resources = inspect_external_resources(path)
    except (OSError, BadZipFile, ElementTree.ParseError, RuntimeError, ValueError):
        return incomplete("office_external_resource_check_failed")
    if resources["blocked"] and not allow_external_resources:
        return incomplete("office_external_resource_blocked")
    executable = soffice_path()

    if not executable:
        return incomplete("libreoffice_missing")
    try:
        with owned_temporary_directory(prefix="openmd-office-") as tmp:
            directory = Path(tmp)
            with (directory / "convert.log").open("wb") as log:
                process = owned_popen([executable, "-env:UserInstallation=" + (directory / "profile").as_uri(), "--headless", "--convert-to", "pdf", "--outdir", str(directory), str(path.resolve())], stdin=subprocess.DEVNULL, stdout=log, stderr=log, **process_options())
                external_fetched = bool(resources["blocked"] and allow_external_resources)
                try:
                    wait_interruptibly(process, 120)
                except BaseException:
                    terminate_quietly(process)
                    raise
            converted = directory / (path.stem + ".pdf")
            if process.returncode or not converted.is_file():
                return incomplete("libreoffice_conversion_failed")
            document = replace(item, url=converted.as_uri(), raw_meta={**item.raw_meta, "local_path": str(converted)})
            from .documents import _office_pdf_origin

            origin = _office_pdf_origin(converted, path)
            extracted = extract_pdf(document, allow_ocr=allow_ocr, _office_origin=origin)
            if not extracted.has_usable_text:
                return incomplete(extracted.error_code or "office_recovery_empty", extracted)
            page_extractions = [{"page": page.get("page"), "route": page.get("route"), "engine": page.get("engine")} for page in extracted.metadata.get("pages", [])]
            formula_metadata = {}
            if path.suffix.lower() == ".docx" and page_extractions and all(page.get("route") == "textlayer" and page.get("status") == "verified" for page in extracted.metadata["pages"]):
                from .native_math import restore_docx_math

                text, formula_metadata = restore_docx_math(path, extracted.text)
                extracted = replace(extracted, text=text)
                if formula_metadata["restored"]:
                    extracted = replace(extracted, source_notes=[*extracted.source_notes, "Native formula structure restored from DOCX OMML only where reliable PDF text matched uniquely, character for character except whitespace; no model was used."])
            return replace(extracted, backend=result.backend, title=item.title, source_notes=[*extracted.source_notes, *external_notes(), "Office loss suspected; rendered locally through existing LibreOffice and extracted page by page."], metadata={**extracted.metadata, "source_format": path.suffix.lstrip("."), "office_loss_check": detection, "office_external_resources_fetched": external_fetched, "office_recovery": {"status": "completed" if extracted.status == "verified" else "partial", "converter": "LibreOffice", "page_extractions": page_extractions, **({"native_formula_restoration": formula_metadata} if formula_metadata else {}), **external_metadata()}})
    except (OSError, ValueError, subprocess.SubprocessError):
        return incomplete("libreoffice_conversion_failed")
