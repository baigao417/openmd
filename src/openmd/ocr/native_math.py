"""Restore supported DOCX formula structure only with unique native text proof."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


_MATH = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_WORD_NAMESPACES = {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                    "http://purl.oclc.org/ooxml/wordprocessingml/main"}
_COMPATIBILITY = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _xml_source(data):
    """Retain namespace scopes needed to interpret mc:Ignorable accurately."""
    scopes, pending, ignored = [], {}, {}
    for event, value in ET.iterparse(BytesIO(data), events=("start", "end", "start-ns")):
        if event == "start-ns":
            pending[value[0]] = value[1]
        elif event == "start":
            names = dict(scopes[-1][0]) if scopes else {}
            names.update(pending)
            pending.clear()
            namespaces = set(scopes[-1][1]) if scopes else set()
            namespaces.update(names.get(prefix, "") for prefix in value.get(_COMPATIBILITY + "Ignorable", "").split())
            ignored[value] = value.tag.partition("}")[0].removeprefix("{") in namespaces
            scopes.append((names, namespaces))
        else:
            scopes.pop()
    return value, ignored


def _permitted_math_sources(roots, ignored) -> list[str]:
    """Prove the entire ancestry, not just absence of known unsafe wrappers.

    This intentionally accepts a small rendering grammar. Properties, extension
    wrappers and other parts cannot acquire identity from matching PDF digits.
    Header/footer math is checked but only main-document math is restored.
    """
    reasons = set()
    for part, root in roots.items():
        if not any(n.tag.rsplit('}', 1)[-1] in {"oMath", "oMathPara"} for n in root.iter()):
            continue
        expected = "document" if part == "word/document.xml" else (
            "hdr" if re.fullmatch(r"word/header[0-9]+\.xml", part) else
            "ftr" if re.fullmatch(r"word/footer[0-9]+\.xml", part) else None)
        namespace = next((ns for ns in _WORD_NAMESPACES if root.tag == "{" + ns + "}" + str(expected)), None)
        if expected is None or namespace is None:
            reasons.add("omml_unapproved_part_or_root")
            continue
        word = "{" + namespace + "}"
        # Every edge on the path to an equation must be explicitly recognized.
        edges = {
            word + "document": {word + "body"},
            **{word + n: {word + "p", word + "tbl"} for n in ("body", "hdr", "ftr", "tc")},
            word + "tbl": {word + "tr"}, word + "tr": {word + "tc"},
            word + "p": {word + "r", _MATH + "oMathPara", _MATH + "oMath"},
            # Inline (run-level) oMath is a sibling of w:r inside w:p. CT_R
            # itself is not an equation container.
            _MATH + "oMathPara": {_MATH + "oMath"},
        }
        def visit(node, permitted):
            if node.tag.rsplit('}', 1)[-1] in {"oMath", "oMathPara"}:
                if not permitted or node.tag not in {_MATH + "oMath", _MATH + "oMathPara"}:
                    reasons.add("omml_unapproved_ancestor")
            for child in node:
                visit(child, permitted and child.tag in edges.get(node.tag, set()) and not ignored.get(child, False))
        visit(root, not ignored.get(root, False))
    return sorted(reasons)


def _ambiguous_rendering(roots) -> list[str]:
    """A source-wide veto avoids guessing a converter's rendered branch."""
    reasons = set()
    for root in roots:
        for node in root.iter():
            if node.tag == _COMPATIBILITY + "AlternateContent":
                reasons.add("alternate_content")
            for namespace in _WORD_NAMESPACES:
                prefix = "{" + namespace + "}"
                if not node.tag.startswith(prefix):
                    continue
                kind = node.tag.removeprefix(prefix)
                if kind in {"ins", "del"} or kind.startswith(("moveFrom", "moveTo")) or kind.endswith("PrChange"):
                    reasons.add("revision_markup")
                if kind in {"fldSimple", "fldChar", "instrText", "delInstrText"}:
                    reasons.add("field_code")
                if kind in {"vanish", "webHidden"}:
                    reasons.add("hidden_content")
                if kind in {"altChunk", "subDoc", "dataBinding"}:
                    reasons.add("dynamic_content")
    return sorted(reasons)


def _formula(node) -> tuple[str, str]:
    """Return extraction-order characters and explicit source math structure.

    This deliberately supports a small OMML subset. Unknown structures cannot
    be flattened speculatively. Formatting properties do not supply characters.
    """
    kind = node.tag.removeprefix(_MATH)

    def child(name):
        matches = node.findall(_MATH + name)
        if len(matches) != 1:
            raise ValueError("missing_or_duplicate_math_component")
        return _formula(matches[0])

    if node.tag == _MATH + "t":
        value = node.text or ""
        # Do not interpret literal TeX syntax from a text run as markup.
        if any(c in value for c in "\\{}$&#_%^"):
            raise ValueError("literal_math_markup")
        return value, value
    containers = {"oMath", "r", "e", "sub", "sup", "num", "den"}
    components = {"sSub": {"e", "sub", "sSubPr"}, "sSup": {"e", "sup", "sSupPr"},
                  "sSubSup": {"e", "sub", "sup", "sSubSupPr"},
                  "f": {"num", "den", "fPr"}, "nary": {"sub", "sup", "e", "naryPr"}}
    if kind not in containers | components.keys() or not node.tag.startswith(_MATH):
        raise ValueError("unsupported_math_structure")
    if kind in components and any(c.tag not in {_MATH + n for n in components[kind]} for c in node):
        raise ValueError("unsupported_math_component")
    if kind in {"sSub", "sSup", "sSubSup"}:
        plain, markup = child("e")
        for name, operator in (("sub", "_"), ("sup", "^")):
            if name == "sub" and kind == "sSup" or name == "sup" and kind == "sSub":
                continue
            raw, rendered = child(name)
            plain += raw
            markup += operator + "{" + rendered + "}"
        return plain, markup
    if kind == "f":
        fraction_type = node.find(_MATH + "fPr/" + _MATH + "type")
        if fraction_type is not None and fraction_type.get(_MATH + "val") != "bar":
            raise ValueError("unsupported_fraction_type")
        numerator, denominator = child("num"), child("den")
        return numerator[0] + denominator[0], "\\frac{" + numerator[1] + "}{" + denominator[1] + "}"
    if kind == "nary":
        symbol = node.find(_MATH + "naryPr/" + _MATH + "chr")
        symbol = symbol.get(_MATH + "val", "") if symbol is not None else ""
        if symbol not in {"∑", "∏", "∫", "∬", "∭", "⋃", "⋂"}:
            raise ValueError("unsupported_nary_symbol")
        lower, upper, expression = child("sub"), child("sup"), child("e")
        return symbol + lower[0] + upper[0] + expression[0], symbol + "_{" + lower[1] + "}^{" + upper[1] + "}" + expression[1]
    pairs = [_formula(c) for c in node if c.tag not in {
        _MATH + "rPr", "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr"}]
    return "".join(p[0] for p in pairs), "".join(p[1] for p in pairs)


def restore_docx_math(path: Path, text: str) -> tuple[str, dict]:
    """Never change characters without a unique, whitespace-only native match.

    Caller must already have proved every output page's text layer reliable.
    No OCR, URL resolution, normalization, numerical correction or guessing.
    """
    receipt = {"source": "docx_omml", "formulas": 0, "restored": 0, "unmatched": 0, "unsupported": 0,
               "match_rule": "unique_raw_characters_ignoring_whitespace_within_paragraph"}
    try:
        with ZipFile(path) as archive:
            roots, ignored = {}, {}
            names = archive.namelist()
            if len(names) != len(set(names)):
                receipt.update(disabled=True, reason="ambiguous_source_parts", disabled_reasons=["duplicate_source_part"])
                return text, receipt
            for name in names:
                data = archive.read(name)
                # OPC XML parts need not use .xml names. Unrecognized XML can
                # contain equations too; media bytes grant no source identity.
                looks_xml = data.lstrip().startswith((b"<", b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
                if name.lower().endswith((".xml", ".vml")) or looks_xml:
                    roots[name], scope = _xml_source(data)
                    ignored.update(scope)
            root = roots["word/document.xml"]
    except (OSError, BadZipFile, KeyError, ET.ParseError):
        receipt["source_unavailable"] = True
        return text, receipt
    reasons = _ambiguous_rendering(roots.values())
    if reasons:
        receipt.update({"disabled": True, "reason": "ambiguous_document_rendering", "disabled_reasons": reasons})
        return text, receipt
    reasons = _permitted_math_sources(roots, ignored)
    if reasons:
        receipt.update({"disabled": True, "reason": "unapproved_omml_rendering_path", "disabled_reasons": reasons})
        return text, receipt
    offsets = [i for i, c in enumerate(text) if not c.isspace()]
    compact = "".join(text[i] for i in offsets)
    replacements = []
    for node in root.iter(_MATH + "oMath"):
        receipt["formulas"] += 1
        try:
            plain, markup = _formula(node)
        except ValueError:
            receipt["unsupported"] += 1
            continue
        needle = "".join(c for c in plain if not c.isspace())
        start = compact.find(needle)
        if len(needle) < 4 or start < 0 or compact.find(needle, start + 1) >= 0:
            receipt["unmatched"] += 1
            continue
        end = start + len(needle)
        # Native PDF extraction uses blank lines for paragraph boundaries.
        # Whitespace-insensitive matching must never bridge those boundaries
        # (or a page break), even if the combined characters occur only once.
        candidate = text[offsets[start]:offsets[end - 1] + 1]
        if re.search(r"\n[ \t\r]*\n|\f", candidate):
            receipt["unmatched"] += 1
            receipt["paragraph_boundary_rejections"] = receipt.get("paragraph_boundary_rejections", 0) + 1
            continue
        replacements.append((offsets[start], offsets[end - 1] + 1, "$" + markup + "$"))
    # Repeated source formulas or nested/overlapping matches are not unique proof.
    unique = [r for r in replacements if sum(r[0] < s[1] and s[0] < r[1] for s in replacements) == 1]
    receipt["unmatched"] += len(replacements) - len(unique)
    for start, end, replacement in sorted(unique, reverse=True):
        text = text[:start] + replacement + text[end:]
        receipt["restored"] += 1
    return text, receipt
