"""Generic AST/text scanner for the declared single-owner surfaces."""
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


def _call_name(node: ast.Call) -> str:
    return ast.unparse(node.func)


def scan_shared_surfaces(source_root: Path, manifest_path: Path) -> list[SurfaceViolation]:
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    violations: list[SurfaceViolation] = []
    for surface in manifest["surfaces"]:
        name = surface["name"]
        owner = surface["owner"]
        allowed = set(surface.get("allowed_modules", [owner]))
        extensions = set(surface.get("extensions", [".py"]))
        text_patterns = [re.compile(pattern) for pattern in surface.get("text_patterns", [])]
        ast_calls = set(surface.get("ast_calls", []))
        ast_context = tuple(str(value).lower() for value in surface.get("ast_context_any", []))
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.suffix not in extensions:
                continue
            relative = path.relative_to(source_root).as_posix()
            if relative in allowed:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                continue
            for pattern in text_patterns:
                match = pattern.search(text)
                if match:
                    violations.append(SurfaceViolation(
                        name, relative, f"text:{match.group(0)}", owner,
                        surface["owner_function"], surface["guidance"],
                    ))
                    break
            if ast_calls and path.suffix == ".py" and (not ast_context or any(value in text.lower() for value in ast_context)):
                try:
                    tree = ast.parse(text, filename=str(path))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and _call_name(node) in ast_calls:
                        violations.append(SurfaceViolation(
                            name, relative, f"ast:{_call_name(node)}", owner,
                            surface["owner_function"], surface["guidance"],
                        ))
    return violations


def format_violations(violations: list[SurfaceViolation]) -> str:
    return "\n".join(
        f"{item.surface}: {item.path} contains {item.evidence}; "
        f"call {item.owner}::{item.owner_function} ({item.guidance})"
        for item in violations
    )
