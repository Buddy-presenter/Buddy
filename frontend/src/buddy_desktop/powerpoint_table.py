"""Resolve extracted table phrases against actual Office cells, never whole-table guesses."""

import math
from typing import Any

from buddy_desktop.powerpoint_contract import PowerPointError
from buddy_desktop.slide_contract import Region


def table_region(source: Any, phrase: str) -> Region:
    table = source.Table
    cells = []
    texts = []
    seen: set[tuple[float, ...]] = set()
    count = int(table.Rows.Count) * int(table.Columns.Count)
    if count > 10000:
        raise PowerPointError("The source table exceeds the cell limit.")
    for row in range(1, int(table.Rows.Count) + 1):
        for column in range(1, int(table.Columns.Count) + 1):
            cell = table.Cell(row, column).Shape
            bounds = tuple(map(float, (cell.Left, cell.Top, cell.Width, cell.Height)))
            if bounds in seen:
                # Office exposes the same merged cell at each covered row/column.
                # OOXML contains empty continuation cells, so preserve that separator.
                cells.append(cell)
                texts.append("")
                continue
            seen.add(bounds)
            text = str(cell.TextFrame.TextRange.Text).replace("\r", "\n").replace("\v", "\n")
            cells.append(cell)
            texts.append(text)
    joined = "\n".join(texts)
    if not phrase or joined.count(phrase) != 1:
        raise PowerPointError("The selected table phrase is missing or ambiguous.")
    start = joined.index(phrase)
    end = start + len(phrase)
    cursor = 0
    selected = []
    for cell, text in zip(cells, texts, strict=True):
        if cursor < end and cursor + len(text) > start:
            selected.append(tuple(map(float, (cell.Left, cell.Top, cell.Width, cell.Height))))
        cursor += len(text) + 1
    if not selected or not all(math.isfinite(v) for bounds in selected for v in bounds):
        raise PowerPointError("PowerPoint could not measure the selected table cells.")
    x = min(b[0] for b in selected)
    y = min(b[1] for b in selected)
    right = max(b[0] + b[2] for b in selected)
    bottom = max(b[1] + b[3] for b in selected)
    if right <= x or bottom <= y:
        raise PowerPointError("The selected table cells have no visible area.")
    return Region(x, y, right - x, bottom - y)
