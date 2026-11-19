"""AST behavior checks for declared single-owner shared surfaces."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SurfaceViolation:
    surface: str
    path: str
    evidence: str
    owner: str
    owner_function: str
    guidance: str


def _call_parts(node: ast.Call) -> tuple[str | None, str | None]:
    function = node.func
    if isinstance(function, ast.Attribute):
        root = function.value.id if isinstance(function.value, ast.Name) else None
        return root, function.attr
    if isinstance(function, ast.Name):
        return None, function.id
    return None, None


def _literal_strings(node: ast.AST) -> set[str]:
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)}


def _has_extension_hash(tree: ast.AST) -> bool:
    calls = [_call_parts(node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    has_hash = any(root == "hashlib" and attr == "sha256" for root, attr in calls)
    has_walk = any(attr in {"rglob", "glob", "walk"} for _, attr in calls)
    has_read = any(attr in {"read_bytes", "read_text", "open"} for _, attr in calls)
    names = {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.name.lower() for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    attrs = {node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    strings = {value.lower() for value in _literal_strings(tree)}
    extension_context = any("extension_hash" in value for value in names | attrs) or any(
        value.endswith("manifest.json") or "extension" in value for value in strings
    )
    return has_hash and has_walk and has_read and extension_context


def _has_patch_table(tree: ast.AST) -> bool:
    calls = [_call_parts(node) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    has_edit = any(attr in {"replace", "count"} for _, attr in calls)
    has_fields = False
    patch_context = any(
        isinstance(node, ast.Name) and (
            node.id.lower() in {"patch", "patches", "edit", "edits", "loader", "loaders"}
            or node.id.lower().endswith(("_patch", "_patches", "_edit", "_edits", "_loader", "_loaders"))
        )
        for node in ast.walk(tree)
    ) or any(value.lower() in {"patch", "patches", "edits", "loader", "original", "replacement", "versions"} for value in _literal_strings(tree))
    def patch_fields(node: ast.AST) -> bool:
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            return {"original", "replacement"}.issubset(keys) and bool(keys.intersection({"count", "version", "versions"}))
        if isinstance(node, (ast.List, ast.Tuple)):
            constants = [item for item in ast.walk(node) if isinstance(item, ast.Constant)]
            return sum(isinstance(item.value, str) for item in constants) >= 3 and any(isinstance(item.value, int) for item in constants)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if {"original", "replacement"}.issubset(keys) and keys.intersection({"count", "version", "versions"}):
                has_fields = True
        elif isinstance(node, (ast.List, ast.Tuple)):
            if patch_fields(node):
                has_fields = True
    return has_edit and has_fields and patch_context


def _has_preference_write(tree: ast.AST) -> bool:
    strings = _literal_strings(tree)
    if not any(value.replace("\\", "/").endswith("preferences.json") for value in strings):
        return False
    return any(attr in {"write_text", "write_bytes", "open", "dump", "dumps", "replace"}
               for _, attr in (_call_parts(node) for node in ast.walk(tree) if isinstance(node, ast.Call)))


def _has_process_start(tree: ast.AST) -> bool:
    aliases = {"subprocess"}
    popen_names = {"Popen"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "subprocess":
                    aliases.add(item.asname or item.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for item in node.names:
                if item.name == "Popen":
                    popen_names.add(item.asname or item.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        root, attr = _call_parts(node)
        if attr == "Popen" and root in aliases:
            return True
        if root is None and attr in popen_names:
            return True
    return False


def _has_check_status_aggregate(tree: ast.AST) -> bool:
    strings = _literal_strings(tree)
    if not {"downgrade", "ok", "unknown"}.issubset(strings):
        return False
    has_check = any(isinstance(node, ast.Name) and node.id in {"Check", "check", "checks"} for node in ast.walk(tree))
    has_status = any(isinstance(node, ast.Attribute) and node.attr == "status" for node in ast.walk(tree))
    has_aggregate = any(
        (isinstance(node, ast.Call) and _call_parts(node)[1] in {"any", "all"}) or isinstance(node, ast.IfExp)
        for node in ast.walk(tree)
    )
    return has_check and has_status and has_aggregate


def _has_title_suffix_cleaning(tree: ast.AST) -> bool:
    aliases = {"豆包", "Google Gemini", "ChatGPT", "Gemini", "Qwen", "千问", "元宝", "DeepSeek", "Grok"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        root, attr = _call_parts(node)
        if root != "re" or attr != "sub" or not node.args:
            continue
        pattern = node.args[0]
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
            continue
        if "$" in pattern.value and any(alias in pattern.value for alias in aliases):
            return True
        if "$" in pattern.value and any(token in pattern.value for token in ("[-", "–", "—", "|·")):
            return True
    return False


def _status_result_target(node: ast.AST) -> bool:
    """Match status['result'/'receipt'] or a field nested under that value."""
    while isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and key.value in {"result", "receipt"}:
            return isinstance(node.value, ast.Name) and node.value.id == "status"
        node = node.value
    return False


def _has_raw_receipt_copy(tree: ast.AST) -> bool:
    sensitive = {"next_action", "error", "content", "text", "body", "raw_text", "markdown"}

    def raw_sensitive_read(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and _call_parts(child) == ("result", "get"):
                if child.args and isinstance(child.args[0], ast.Constant) and child.args[0].value in sensitive:
                    return True
            if isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id == "result":
                key = child.slice
                if isinstance(key, ast.Constant) and key.value in sensitive:
                    return True
        return False

    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))]
    for assignment in assignments:
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        value = assignment.value
        if not any(_status_result_target(target) for target in targets):
            continue
        if not isinstance(value, ast.DictComp):
            # Also catch field-by-field copies such as status['result']['error'] = result.get('error').
            if raw_sensitive_read(value):
                return True
            continue
        if not raw_sensitive_read(value):
            continue
        key_vars = {node.id for node in ast.walk(value.key) if isinstance(node, ast.Name)}
        for generator in value.generators:
            if not (key_vars & {node.id for node in ast.walk(generator.target) if isinstance(node, ast.Name)}):
                continue
            fields = {item.value for item in ast.walk(generator.iter)
                      if isinstance(item, ast.Constant) and isinstance(item.value, str)}
            if fields & sensitive:
                return True
    return False


_DETECTORS = {
    "extension_hash": ("ast:hashlib.sha256+extension traversal", _has_extension_hash),
    "patch_table": ("ast:patch table+source editing", _has_patch_table),
    "preference_write": ("ast:preferences.json write", _has_preference_write),
    "process_start": ("ast:subprocess.Popen", _has_process_start),
    "check_status_aggregate": ("ast:Check status aggregate", _has_check_status_aggregate),
    "title_suffix_cleaning": ("ast:site title suffix regex", _has_title_suffix_cleaning),
    "raw_receipt_copy": ("ast:raw result fields copied into status receipt", _has_raw_receipt_copy),
}


def _text_detects(detector_name: str, text: str) -> bool:
    if detector_name != "patch_table":
        return False
    return bool(
        re.search(r"(?:PATCHES|patches|EDIT[S]?|loader)", text)
        and re.search(r"\.replace\s*\(", text)
        and re.search(r"(?:count|version|original|replacement)", text, re.I)
    )


def scan_shared_surfaces(source_root: Path, manifest_path: Path) -> list[SurfaceViolation]:
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    violations: list[SurfaceViolation] = []
    for surface in manifest["surfaces"]:
        detector_name = surface["detector"]
        evidence, detector = _DETECTORS[detector_name]
        allowed = set(surface.get("allowed_modules", [surface["owner"]]))
        extensions = set(surface.get("extensions", [".py"]))
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.suffix not in extensions:
                continue
            relative = path.relative_to(source_root).as_posix()
            if relative in allowed:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
                tree = ast.parse(text, filename=str(path)) if path.suffix == ".py" else None
            except (UnicodeDecodeError, SyntaxError):
                continue
            matched = detector(tree) if tree is not None else _text_detects(detector_name, text)
            if matched:
                violations.append(SurfaceViolation(
                    surface["name"], relative, evidence, surface["owner"],
                    surface["owner_function"], surface["guidance"],
                ))
    return violations


def format_violations(violations: list[SurfaceViolation]) -> str:
    return "\n".join(
        f"{item.surface}: {item.path} contains {item.evidence}; "
        f"call {item.owner}::{item.owner_function} ({item.guidance})"
        for item in violations
    )
