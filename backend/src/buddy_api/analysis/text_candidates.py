"""Source-measured PDF text entities selectable by ID, with no model-estimated geometry."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import re
from contextlib import closing
from typing import Any, Literal

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]
from pydantic import Field

from buddy_api.analysis.contracts import Digest, Model, Region

MAX_CHARACTERS = 200_000
MAX_CANDIDATES = 2000
MAX_CANDIDATE_TEXT = 80_000
type Glyph = tuple[float, float, float, float, str, int, float, float, float, int]
type Line = tuple[str, Region, float, float, float, int]

_BULLET = re.compile(r"^\s*(?:[•◦▪‣⁃∙●○■□◆▶►‒–—-�]\s+|\(?\d{1,3}[.)]\s+|[A-Za-z][.)]\s+)")
_SPACE = re.compile(r"\s+")


def _table_row_groups(lines: list[Line]) -> dict[int, tuple[int, ...]]:
    """Recognize repeated, aligned multi-column rows without treating prose as cells."""
    if len(lines) < 6:
        return {}
    median_height = sorted(line[3] for line in lines)[len(lines) // 2]
    row_tolerance = max(3.0, median_height * 0.75)
    bands: list[list[int]] = []
    for index, line in enumerate(lines):
        center = line[1].y + line[1].height / 2
        if bands:
            last = bands[-1]
            last_center = sum(lines[item][1].y + lines[item][1].height / 2 for item in last) / len(
                last
            )
            if abs(center - last_center) <= row_tolerance:
                last.append(index)
                continue
        bands.append([index])

    table_bands = [band for band in bands if len(band) >= 2]
    if len(table_bands) < 3:
        return {}
    # Centered cell values move their left edge as their text changes. A bounded
    # glyph-relative tolerance associates those values with the same column.
    column_tolerance = max(4.0, median_height * 2.5)
    anchors: list[float] = []
    for start in sorted(lines[index][1].x for band in table_bands for index in band):
        if not anchors or start - anchors[-1] > column_tolerance:
            anchors.append(start)
        else:
            anchors[-1] = (anchors[-1] + start) / 2

    def column_for(index: int) -> int:
        x = lines[index][1].x
        candidates = [
            (abs(x - anchor), column)
            for column, anchor in enumerate(anchors)
            if abs(x - anchor) <= column_tolerance
        ]
        return min(candidates)[1] if candidates else -1

    qualified = [
        band
        for band in table_bands
        if len({column_for(index) for index in band if column_for(index) >= 0}) >= 2
    ]
    if len(qualified) < 3:
        return {}

    # Two columns are common in ordinary slide prose. Parallel baselines alone
    # are not enough to call them table rows. Require compact, cell-like content
    # (or an explicit numeric value column) before combining two-column text.
    # Three or more stable columns already provide substantially stronger table
    # structure and continue to use the general rule below.
    qualified_columns = {
        column_for(index) for band in qualified for index in band if column_for(index) >= 0
    }
    if len(qualified_columns) == 2:
        compact_cells = all(
            len(lines[index][0]) <= 48 and len(lines[index][0].split()) <= 7
            for band in qualified
            for index in band
        )
        numeric_column = any(
            sum(
                bool(re.fullmatch(r"[\s$€£¥+−-]?\d[\d,.%$€£¥\s()−/+-]*", lines[index][0]))
                for band in qualified
                for index in band
                if column_for(index) == column
            )
            >= max(
                2,
                (sum(column_for(index) == column for band in qualified for index in band) + 1) // 2,
            )
            for column in qualified_columns
        )
        if not compact_cells and not numeric_column:
            return {}

    # Require two stable columns across at least three neighboring rows. A few
    # aligned labels in a page header therefore cannot turn into a table.
    output: dict[int, tuple[int, ...]] = {}
    run: list[list[int]] = []
    for band in qualified:
        if run:
            previous_bottom = max(lines[index][1].y + lines[index][1].height for index in run[-1])
            current_top = min(lines[index][1].y for index in band)
            if current_top - previous_bottom > median_height * 3.5:
                _commit_table_run(run, lines, column_for, output)
                run = []
        run.append(band)
    _commit_table_run(run, lines, column_for, output)
    return output


def _commit_table_run(
    run: list[list[int]],
    lines: list[Line],
    column_for: Any,
    output: dict[int, tuple[int, ...]],
) -> None:
    if len(run) < 3:
        return
    frequencies: dict[int, int] = {}
    for band in run:
        for column in {column_for(index) for index in band}:
            if column >= 0:
                frequencies[column] = frequencies.get(column, 0) + 1
    stable_columns = {column for column, count in frequencies.items() if count >= 3}
    if len(stable_columns) < 2:
        return
    for band in run:
        stable_cells = [index for index in band if column_for(index) in stable_columns]
        if len(stable_cells) < 2:
            continue
        left = min(lines[index][1].x for index in stable_cells)
        right = max(lines[index][1].x + lines[index][1].width for index in stable_cells)
        # Once a row is structurally recognized, include all source text between
        # its stable outer columns. This retains centered/offset cell text whose
        # left edge varies more than the column-clustering tolerance.
        cells = tuple(
            index
            for index in band
            if left <= lines[index][1].x + lines[index][1].width / 2 <= right
        )
        if len(cells) >= 2:
            output[cells[0]] = cells


def _parallel_text_columns(lines: list[Line], excluded: set[int]) -> list[tuple[int, ...]]:
    """Find repeated, spatially separate text tracks for column prose grouping."""
    available = [index for index in range(len(lines)) if index not in excluded]
    if len(available) < 6:
        return []
    median_height = sorted(lines[index][3] for index in available)[len(available) // 2]
    row_tolerance = max(3.0, median_height * 0.75)
    bands: list[list[int]] = []
    for index in available:
        center = lines[index][1].y + lines[index][1].height / 2
        if bands:
            last = bands[-1]
            last_center = sum(lines[item][1].y + lines[item][1].height / 2 for item in last) / len(
                last
            )
            if abs(center - last_center) <= row_tolerance:
                last.append(index)
                continue
        bands.append([index])
    paired_bands = [band for band in bands if len(band) >= 2]
    if len(paired_bands) < 3:
        return []

    tolerance = max(4.0, median_height * 2.5)
    anchors: list[float] = []
    for start in sorted(lines[index][1].x for band in paired_bands for index in band):
        if not anchors or start - anchors[-1] > tolerance:
            anchors.append(start)
        else:
            anchors[-1] = (anchors[-1] + start) / 2

    def column_for(index: int) -> int:
        x = lines[index][1].x
        matches = [
            (abs(x - anchor), column)
            for column, anchor in enumerate(anchors)
            if abs(x - anchor) <= tolerance
        ]
        return min(matches)[1] if matches else -1

    stable = {
        column
        for column in range(len(anchors))
        if sum(any(column_for(index) == column for index in band) for band in paired_bands) >= 3
    }
    if len(stable) < 2:
        return []
    tracks = [
        tuple(index for index in available if column_for(index) == column)
        for column in sorted(stable)
    ]
    return [track for track in tracks if len(track) >= 3]


class TextCandidate(Model):
    id: str
    page: int
    kind: Literal["title", "heading", "bullet", "paragraph", "table_row"]
    text: str = Field(min_length=1, max_length=16_000)
    region: Region


class TextCandidates(Model):
    version: str = "pdf-text-entities-2"
    source_sha256: Digest
    pages: tuple[tuple[int, int], ...]
    candidates: tuple[TextCandidate, ...]
    coverage_limited: bool = False


def candidate_page(
    page: Any,
    page_number: int,
    source_sha256: str,
    *,
    omitted: list[bool] | None = None,
) -> list[TextCandidate]:
    """Build source-measured text entities (titles, paragraphs and complete bullets).

    PDF character geometry is grouped into physical lines first. Adjacent lines are
    then joined using baseline spacing, indentation and glyph size so wrapped prose
    produces one candidate with the union of its own glyph bounds.
    """
    width, height = (math.ceil(value * 2) for value in page.get_size())
    if page.get_rotation() != 0:
        if omitted is not None:
            omitted[0] = True
        return []
    if max(width, height) > 8192 or width * height > 16_777_216:
        if omitted is not None:
            omitted[0] = True
        return []
    with closing(page.get_textpage()) as textpage, closing(page.render(scale=2)) as bitmap:
        character_count = textpage.count_chars()
        if character_count > MAX_CHARACTERS and omitted is not None:
            omitted[0] = True
        converter = bitmap.get_posconv(page)
        glyphs = []
        codes = [
            raw.FPDFText_GetUnicode(textpage, index)
            for index in range(min(character_count, MAX_CHARACTERS))
        ]
        source_chars = [chr(code) if 0 < code <= 0x10FFFF else "" for code in codes]
        for index, code in enumerate(codes):
            if not code or code == 0xFFFD or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                continue
            char = chr(code)
            if not char.isprintable() or char.isspace():
                continue
            # Accept horizontal source baselines only; no guessing for rotated text.
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if min(abs(angle), abs(angle - math.tau)) > 0.05:
                if omitted is not None:
                    omitted[0] = True
                continue
            left, bottom, right, top = textpage.get_charbox(index)
            points = [converter.to_bitmap(x, y) for x, y in ((left, top), (right, bottom))]
            x1, x2 = sorted(float(point[0]) for point in points)
            y1, y2 = sorted(float(point[1]) for point in points)
            if x2 <= x1 or y2 <= y1 or min(x1, y1) < 0 or x2 > width or y2 > height:
                continue
            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
            if not raw.FPDFText_GetCharOrigin(textpage, index, origin_x, origin_y):
                continue
            baseline = float(converter.to_bitmap(origin_x.value, origin_y.value)[1])
            glyphs.append(
                (
                    x1,
                    y1,
                    x2,
                    y2,
                    char,
                    index,
                    baseline,
                    float(y2 - y1),
                    float(raw.FPDFText_GetFontSize(textpage, index)),
                    int(raw.FPDFText_GetFontWeight(textpage, index)),
                )
            )
    # PDF glyph origins provide actual baselines, including descenders. Nearby rows
    # are compared, keeping the scan bounded even for many disconnected lines.
    rows: list[list[Glyph]] = []
    for glyph in sorted(glyphs, key=lambda g: (g[6], g[0])):
        row = next(
            (
                items
                for items in reversed(rows[-64:])
                if abs(items[0][6] - glyph[6]) <= 2.0
                or (
                    0.75 <= (glyph[3] - glyph[1]) / max(items[0][3] - items[0][1], 1.0) <= 1.33
                    and max(
                        0.0,
                        min(items[0][3], glyph[3]) - max(items[0][1], glyph[1]),
                    )
                    >= min(items[0][3] - items[0][1], glyph[3] - glyph[1]) * 0.65
                )
                or (
                    glyph[4] in "-‐‑–—.,;:!?'’”)]}"
                    and abs(
                        (glyph[1] + glyph[3]) / 2
                        - sum((item[1] + item[3]) / 2 for item in items) / len(items)
                    )
                    <= sorted(item[3] - item[1] for item in items)[len(items) // 2] * 0.6
                    and max(
                        0.0,
                        glyph[0] - max(item[2] for item in items),
                        min(item[0] for item in items) - glyph[2],
                    )
                    <= sorted(item[3] - item[1] for item in items)[len(items) // 2] * 3.0
                )
            ),
            None,
        )
        if row is None:
            rows.append([glyph])
        else:
            row.append(glyph)
    glyph_lines: list[list[Glyph]] = []
    for row in rows:
        segment: list[Glyph] = []
        for glyph in sorted(row, key=lambda g: g[0]):
            prefix = "".join(item[4] for item in segment).strip()
            is_list_prefix = bool(re.fullmatch(r"(?:\d{1,3}[.)]|[A-Za-z][.)])", prefix))
            is_attached_punctuation = glyph[4] in ".,;:!?)]}" or is_list_prefix
            typical_glyph_height = max(
                glyph[3] - glyph[1],
                segment[-1][3] - segment[-1][1] if segment else 0.0,
            )
            if (
                segment
                # Some PDF generators split one visual line into adjacent text
                # objects (for example, before an em dash). Rejoin nearby spans;
                # real columns remain separated by a much wider gutter.
                and glyph[0] - segment[-1][2] > typical_glyph_height * 1.5
                and not is_attached_punctuation
            ):
                glyph_lines.append(segment)
                segment = []
            segment.append(glyph)
        if segment:
            glyph_lines.append(segment)
    physical_lines: list[Line] = []
    for glyph_line in sorted(glyph_lines, key=lambda items: (items[0][1], items[0][0])):
        words = []
        previous_glyph = None
        for glyph in glyph_line:
            if previous_glyph is not None and (
                (
                    0 < glyph[5] - previous_glyph[5] <= 16
                    and any(
                        char.isspace() for char in source_chars[previous_glyph[5] + 1 : glyph[5]]
                    )
                )
                or glyph[0] - previous_glyph[2]
                > max(previous_glyph[3] - previous_glyph[1], glyph[3] - glyph[1]) * 0.65
            ):
                words.append(" ")
            words.append(glyph[4])
            previous_glyph = glyph
        text = "".join(words).strip()
        if not text or not any(char.isalnum() for char in text):
            continue
        left, top = min(g[0] for g in glyph_line), min(g[1] for g in glyph_line)
        right, bottom = max(g[2] for g in glyph_line), max(g[3] for g in glyph_line)
        region = Region(x=left, y=top, width=right - left, height=bottom - top)
        baseline = sum(g[6] for g in glyph_line) / len(glyph_line)
        glyph_height = sorted(g[7] for g in glyph_line)[len(glyph_line) // 2]
        font_sizes = sorted(g[8] for g in glyph_line if g[8] > 0)
        font_size = font_sizes[len(font_sizes) // 2] if font_sizes else glyph_height
        font_weight = max(g[9] for g in glyph_line)
        physical_lines.append((text, region, baseline, glyph_height, font_size, font_weight))

    # Some generators place a hyphen and the following word in separate text rows
    # despite drawing them on one baseline. Join only the adjacent, overlapping pair.
    merged_physical_lines: list[Line] = []
    for line in physical_lines:
        if merged_physical_lines and line[0].lstrip().startswith("-"):
            previous = merged_physical_lines[-1]
            scale = max(previous[3], line[3])
            overlap = max(
                0.0,
                min(previous[1].y + previous[1].height, line[1].y + line[1].height)
                - max(previous[1].y, line[1].y),
            )
            gap = line[1].x - (previous[1].x + previous[1].width)
            if overlap >= min(previous[1].height, line[1].height) * 0.65 and -scale <= gap <= scale:
                left = min(previous[1].x, line[1].x)
                top = min(previous[1].y, line[1].y)
                right = max(
                    previous[1].x + previous[1].width,
                    line[1].x + line[1].width,
                )
                bottom = max(
                    previous[1].y + previous[1].height,
                    line[1].y + line[1].height,
                )
                merged_physical_lines[-1] = (
                    previous[0] + line[0],
                    Region(x=left, y=top, width=right - left, height=bottom - top),
                    previous[2],
                    max(previous[3], line[3]),
                    max(previous[4], line[4]),
                    max(previous[5], line[5]),
                )
                continue
        merged_physical_lines.append(line)
    physical_lines = merged_physical_lines

    # PDF producers sometimes put a numbered-list marker in a separate text object.
    # Rejoin a bare number with the adjacent punctuation/text only on the same row.
    joined_lines: list[Line] = []
    index = 0
    while index < len(physical_lines):
        line = physical_lines[index]
        if (
            re.fullmatch(r"\d{1,3}", line[0])
            and index + 1 < len(physical_lines)
            and physical_lines[index + 1][0].lstrip().startswith((".", ")"))
        ):
            following = physical_lines[index + 1]
            vertical_overlap = max(
                0.0,
                min(line[1].y + line[1].height, following[1].y + following[1].height)
                - max(line[1].y, following[1].y),
            )
            if vertical_overlap >= min(line[1].height, following[1].height) * 0.65:
                left = min(line[1].x, following[1].x)
                top = min(line[1].y, following[1].y)
                right = max(
                    line[1].x + line[1].width,
                    following[1].x + following[1].width,
                )
                bottom = max(
                    line[1].y + line[1].height,
                    following[1].y + following[1].height,
                )
                joined_lines.append(
                    (
                        line[0] + following[0],
                        Region(x=left, y=top, width=right - left, height=bottom - top),
                        line[2],
                        max(line[3], following[3]),
                        max(line[4], following[4]),
                        max(line[5], following[5]),
                    )
                )
                index += 2
                continue
        joined_lines.append(line)
        index += 1
    physical_lines = joined_lines

    if not physical_lines:
        return []

    # Use font/glyph height and visual placement to distinguish standalone headings
    # from body copy. A cover title is recognized even when it is the only heading.
    typical_height = sorted(item[4] for item in physical_lines)[len(physical_lines) // 2]
    first_top_line = min(
        (item for item in physical_lines if item[1].y <= height * 0.35),
        key=lambda item: (item[1].y, item[1].x),
        default=None,
    )
    kinds: list[str] = []
    for text, region, _baseline, _glyph_height, font_size, font_weight in physical_lines:
        if _BULLET.match(text):
            kinds.append("bullet")
        elif (
            font_size >= typical_height * 1.22
            or (
                # PDFium reports ordinary body fonts as 400; 350 incorrectly treated
                # nearly every line in some PDFs as a heading and split wrapped bullets.
                font_weight >= 650
                and font_size >= typical_height * 0.98
                and region.y <= height * 0.45
            )
            or (
                first_top_line is not None
                and text == first_top_line[0]
                and font_size >= typical_height * 0.92
                # A narrow line at the top is not enough to call it a title: body
                # copy can start there too. For same-size text, require visual centering;
                # larger or bold titles are detected by the font metrics above.
                and abs(region.x + region.width / 2 - width / 2) < width * 0.12
            )
        ):
            kinds.append(
                "title" if text == (first_top_line[0] if first_top_line else None) else "heading"
            )
        else:
            kinds.append("paragraph")

    prominent_headings = [
        index
        for index, (line, kind) in enumerate(zip(physical_lines, kinds, strict=True))
        if kind in {"title", "heading"} and line[1].y <= height * 0.35
    ]
    if prominent_headings and not any(kinds[index] == "title" for index in prominent_headings):
        kinds[prominent_headings[0]] = "title"

    table_rows = _table_row_groups(physical_lines)
    table_cell_indices = {item for cells in table_rows.values() for item in cells}

    # Repeated, separate text tracks mean physical reading order interleaves
    # columns. Group each stable track independently; otherwise retain the
    # original top-to-bottom flow for ordinary single-column content.
    column_tracks = _parallel_text_columns(physical_lines, table_cell_indices)
    column_indices = {index for track in column_tracks for index in track}
    streams = [
        tuple(index for index in range(len(physical_lines)) if index not in column_indices),
        *column_tracks,
    ]
    entities: list[tuple[str, str, list[Line]]] = []
    for stream in streams:
        cursor = 0
        while cursor < len(stream):
            index = stream[cursor]
            if index in table_rows:
                group = sorted(
                    (physical_lines[item] for item in table_rows[index]),
                    key=lambda item: item[1].x,
                )
                entities.append(("table_row", " | ".join(item[0] for item in group), group))
                cursor += 1
                continue
            if index in table_cell_indices:
                cursor += 1
                continue
            line = physical_lines[index]
            kind = kinds[index]
            group = [line]
            next_cursor = cursor + 1
            while next_cursor < len(stream):
                candidate_index = stream[next_cursor]
                # Table cells are owned by the table row emitted by the outer loop.
                # Do not let a preceding prose entity consume one as a wrapped line.
                if candidate_index in table_cell_indices:
                    break
                candidate = physical_lines[candidate_index]
                candidate_kind = kinds[candidate_index]
                previous = group[-1]
                gap = candidate[1].y - (previous[1].y + previous[1].height)
                scale = max(previous[3], candidate[3])
                left_aligned = abs(candidate[1].x - line[1].x) <= scale * (
                    3.5 if kind == "bullet" else 2.5
                )
                centered_headings = (
                    kind in {"title", "heading"}
                    and abs(candidate[1].x + candidate[1].width / 2 - line[1].x - line[1].width / 2)
                    <= scale * 2.5
                )
                aligned = left_aligned or centered_headings
                same_style = 0.72 <= candidate[4] / max(line[4], 1.0) <= 1.4
                close = -scale * 0.15 <= gap <= scale * 1.55
                if not (aligned and same_style and close):
                    break
                if kind == "bullet":
                    if candidate_kind != "paragraph" or _BULLET.match(candidate[0]):
                        break
                    # A continuation line can align with the bullet body after its marker.
                    if candidate[1].x < line[1].x - scale * 0.25:
                        break
                elif kind in {"title", "heading"}:
                    if candidate_kind not in {"title", "heading"}:
                        break
                elif candidate_kind != "paragraph":
                    break
                group.append(candidate)
                next_cursor += 1
            text = _SPACE.sub(" ", " ".join(item[0] for item in group)).strip()
            if kind == "bullet":
                text = _BULLET.sub("", text, count=1).strip()
            if text:
                entities.append((kind, text, group))
            cursor += len(group)
    entities.sort(key=lambda entity: (entity[2][0][1].y, entity[2][0][1].x))

    output: list[TextCandidate] = []
    for kind, text, group in entities:
        if len(text) > 16_000:
            if omitted is not None:
                omitted[0] = True
            continue
        left = min(item[1].x for item in group)
        top = min(item[1].y for item in group)
        right = max(item[1].x + item[1].width for item in group)
        bottom = max(item[1].y + item[1].height for item in group)
        region = Region(x=left, y=top, width=right - left, height=bottom - top)
        identity = json.dumps(
            [source_sha256, page_number, kind, text, region.model_dump()], sort_keys=True
        )
        identifier = hashlib.sha256(identity.encode()).hexdigest()[:24]
        output.append(
            TextCandidate(
                id=f"p{page_number}-{identifier}",
                page=page_number,
                kind=kind,  # type: ignore[arg-type]
                text=text,
                region=region,
            )
        )
    return output


def extract_candidates(content: bytes) -> TextCandidates:
    digest = hashlib.sha256(content).hexdigest()
    if len(content) > 18 * 1024 * 1024:
        raise ValueError("candidate_source_limit")
    sizes = []
    candidates_by_page: list[list[TextCandidate]] = []
    coverage_limited = False
    with pdfium.PdfDocument(content) as document:
        if raw.FPDF_GetSecurityHandlerRevision(document) != -1:
            raise ValueError("candidate_encrypted_pdf")
        if not 1 <= len(document) <= 100:
            raise ValueError("candidate_page_limit")
        for index in range(len(document)):
            with closing(document[index]) as page:
                sizes.append(tuple(math.ceil(value * 2) for value in page.get_size()))
                omitted = [False]
                candidates_by_page.append(candidate_page(page, index + 1, digest, omitted=omitted))
                coverage_limited = coverage_limited or omitted[0]

    # Respect the catalogue bound while distributing retained entities across the
    # entire deck; a dense early page must not consume the whole provider request.
    candidates: list[TextCandidate] = []
    characters = 0
    for offset in range(max((len(page) for page in candidates_by_page), default=0)):
        for page_candidates in candidates_by_page:
            if offset >= len(page_candidates):
                continue
            candidate = page_candidates[offset]
            if (
                len(candidates) >= MAX_CANDIDATES
                or characters + len(candidate.text) > MAX_CANDIDATE_TEXT
            ):
                coverage_limited = True
                continue
            candidates.append(candidate)
            characters += len(candidate.text)
    if sum(len(page) for page in candidates_by_page) > len(candidates):
        coverage_limited = True
    return TextCandidates(
        source_sha256=digest,
        pages=tuple(sizes),
        candidates=tuple(candidates),
        coverage_limited=coverage_limited,
    )
