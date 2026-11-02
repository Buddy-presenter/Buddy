from types import SimpleNamespace as S

import pytest

from buddy_desktop.powerpoint_contract import PowerPointError
from buddy_desktop.powerpoint_layout import pointer_points
from buddy_desktop.powerpoint_table import table_region
from buddy_desktop.slide_contract import Region


def table(rows):
    cells = {
        (r + 1, c + 1): S(
            Shape=S(
                Left=100 + c * 80,
                Top=100 + r * 30,
                Width=80,
                Height=30,
                TextFrame=S(TextRange=S(Text=text)),
            )
        )
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    }
    return S(
        Table=S(
            Rows=S(Count=len(rows)), Columns=S(Count=len(rows[0])), Cell=lambda r, c: cells[r, c]
        )
    )


def test_table_row_phrase_uses_only_actual_matching_cells():
    source = table([["Metric", "Paper", "Ours"], ["SR", "66", "66.24"], ["SPL", "61", "61.51"]])
    assert table_region(source, "SR\n66\n66.24") == Region(100, 130, 240, 30)
    assert table_region(source, "66.24") == Region(260, 130, 80, 30)


def test_table_ambiguous_or_missing_phrase_does_not_highlight_whole_table():
    source = table([["0", "0"], ["SR", "66"]])
    for phrase in ("0", "absent"):
        with pytest.raises(PowerPointError, match="missing or ambiguous"):
            table_region(source, phrase)


def test_merged_cell_repeated_by_office_is_not_an_ambiguous_text_match():
    source = table([["Val_Unseen", "66"], ["Val_Unseen", "61"]])
    original = source.Table.Cell
    merged = original(1, 1)
    merged.Shape.Height = 60
    source.Table.Cell = lambda r, c: merged if c == 1 else original(r, c)
    assert table_region(source, "Val_Unseen") == Region(100, 100, 80, 60)


def test_pointer_ends_outside_text_and_avoids_neighbor_content():
    target = Region(100, 100, 200, 40)
    start, end = pointer_points(target, 960, 540, (Region(40, 100, 55, 40),))
    assert start[0] > 300 and end == (303, 120)
    assert start[1] == end[1]


def test_pointer_refuses_to_cross_crowded_content():
    with pytest.raises(PowerPointError, match="No clear space"):
        pointer_points(Region(100, 100, 200, 40), 960, 540, (Region(0, 0, 960, 540),))
