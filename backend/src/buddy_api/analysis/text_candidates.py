"""Source-measured text lines selectable by ID, with no model-estimated geometry."""

import ctypes
import hashlib
import json
import math
from contextlib import closing
from typing import Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]
from pydantic import Field

from buddy_api.analysis.contracts import Digest, Model, Region

MAX_CHARACTERS = 200_000
MAX_CANDIDATES = 2000
MAX_CANDIDATE_TEXT = 80_000
type Glyph = tuple[float, float, float, float, str, int, float]


class TextCandidate(Model):
    id: str
    page: int
    text: str = Field(min_length=1, max_length=256)
    region: Region


class TextCandidates(Model):
    version: str = "pdf-text-lines-1"
    source_sha256: Digest
    pages: tuple[tuple[int, int], ...]
    candidates: tuple[TextCandidate, ...]


def candidate_page(page: Any, page_number: int, source_sha256: str) -> list[TextCandidate]:
    """Group horizontal glyphs by baseline and spacing, splitting distant columns.

    Vertical or strongly rotated text is deliberately omitted in this first version.
    No semantic grouping of neighboring lines or bullets is attempted.
    """
    width, height = (math.ceil(value * 2) for value in page.get_size())
    if page.get_rotation() != 0:
        return []
    if max(width, height) > 8192 or width * height > 16_777_216:
        raise ValueError("candidate_page_size_limit")
    with closing(page.get_textpage()) as textpage, closing(page.render(scale=2)) as bitmap:
        if textpage.count_chars() > MAX_CHARACTERS:
            raise ValueError("candidate_character_limit")
        converter = bitmap.get_posconv(page)
        glyphs = []
        codes = [
            raw.FPDFText_GetUnicode(textpage, index) for index in range(textpage.count_chars())
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
            glyphs.append((x1, y1, x2, y2, char, index, baseline))
    # PDF glyph origins provide actual baselines, including descenders. Nearby rows
    # are compared, keeping the scan bounded even for many disconnected lines.
    rows: list[list[Glyph]] = []
    for glyph in sorted(glyphs, key=lambda g: (g[6], g[0])):
        row = next(
            (items for items in reversed(rows[-64:]) if abs(items[0][6] - glyph[6]) <= 2.0),
            None,
        )
        if row is None:
            rows.append([glyph])
        else:
            row.append(glyph)
    lines: list[list[Glyph]] = []
    for row in rows:
        segment: list[Glyph] = []
        for glyph in sorted(row, key=lambda g: g[0]):
            if segment and glyph[0] - segment[-1][2] > (glyph[3] - glyph[1]) * 1.5:
                lines.append(segment)
                segment = []
            segment.append(glyph)
        if segment:
            lines.append(segment)
    output = []
    for line in sorted(lines, key=lambda items: (items[0][1], items[0][0])):
        words = []
        previous = None
        for glyph in line:
            if previous is not None and (
                (
                    0 < glyph[5] - previous[5] <= 16
                    and any(char.isspace() for char in source_chars[previous[5] + 1 : glyph[5]])
                )
                or glyph[0] - previous[2]
                > max(previous[3] - previous[1], glyph[3] - glyph[1]) * 0.65
            ):
                words.append(" ")
            words.append(glyph[4])
            previous = glyph
        text = "".join(words).strip()
        if not text or not any(char.isalnum() for char in text) or len(text) > 256:
            continue
        left, top = min(g[0] for g in line), min(g[1] for g in line)
        right, bottom = max(g[2] for g in line), max(g[3] for g in line)
        region = Region(x=left, y=top, width=right - left, height=bottom - top)
        identity = json.dumps(
            [source_sha256, page_number, text, region.model_dump()], sort_keys=True
        )
        identifier = hashlib.sha256(identity.encode()).hexdigest()[:24]
        output.append(
            TextCandidate(
                id=f"p{page_number}-{identifier}", page=page_number, text=text, region=region
            )
        )
    return output


def extract_candidates(content: bytes) -> TextCandidates:
    digest = hashlib.sha256(content).hexdigest()
    if len(content) > 18 * 1024 * 1024:
        raise ValueError("candidate_source_limit")
    candidates = []
    sizes = []
    characters = 0
    with pdfium.PdfDocument(content) as document:
        if raw.FPDF_GetSecurityHandlerRevision(document) != -1:
            raise ValueError("candidate_encrypted_pdf")
        if not 1 <= len(document) <= 100:
            raise ValueError("candidate_page_limit")
        for index in range(len(document)):
            with closing(document[index]) as page:
                sizes.append(tuple(math.ceil(value * 2) for value in page.get_size()))
                candidates.extend(candidate_page(page, index + 1, digest))
                characters = sum(len(item.text) for item in candidates)
                if len(candidates) > MAX_CANDIDATES or characters > MAX_CANDIDATE_TEXT:
                    raise ValueError("candidate_count_limit")
    return TextCandidates(source_sha256=digest, pages=tuple(sizes), candidates=tuple(candidates))
