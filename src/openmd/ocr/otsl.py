"""Convert MiniCPM OTSL cell markers to raw HTML without adding dependencies."""

from __future__ import annotations

import html
import re


def table_html(text: str) -> str:
    value = re.sub(r"^\s*<otsl>|</otsl>\s*$", "", text.strip())
    if not value.startswith(("<fcel>", "<ecel>", "<lcel>", "<ucel>", "<xcel>")):
        value = "<fcel>" + value
    tokens = re.split(r"(<(?:fcel|ecel|lcel|ucel|xcel|nl)>)", value)
    cells, grid = [], {}
    row = col = 0
    current = None
    for token in tokens:
        if token == "<nl>":
            row, col, current = row + 1, 0, None
        elif token in {"<fcel>", "<ecel>"}:
            current = {"row": row, "col": col, "row_end": row, "col_end": col, "text": ""}
            cells.append(current)
            grid[row, col] = current
            col += 1
        elif token in {"<lcel>", "<ucel>", "<xcel>"}:
            owner = grid.get((row, col - 1)) if token == "<lcel>" else grid.get((row - 1, col))
            if owner is None:
                raise ValueError("invalid OTSL span")
            owner["row_end"] = max(owner["row_end"], row)
            owner["col_end"] = max(owner["col_end"], col)
            grid[row, col] = owner
            current = None
            col += 1
        elif token.strip():
            if current is None:
                raise ValueError("invalid OTSL content")
            current["text"] += token
    if not cells:
        raise ValueError("empty OTSL table")
    lines = ["<table>"]
    for index in range(max(c["row"] for c in cells) + 1):
        lines.append("<tr>")
        for cell in (c for c in cells if c["row"] == index):
            attrs = ""
            for attr, start, end in (("rowspan", "row", "row_end"), ("colspan", "col", "col_end")):
                span = cell[end] - cell[start] + 1
                if span > 1:
                    attrs += f' {attr}="{span}"'
            lines.append(f"<td{attrs}>{html.escape(cell['text'].strip())}</td>")
        lines.append("</tr>")
    return "\n".join([*lines, "</table>"])
