"""Resolve model targets against PDFium geometry before storing an action.

Model coordinates are only coarse search hints. Highlight rectangles come from source
glyph/word geometry; visual zoom rectangles snap to a unique PDF object or are omitted.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import subprocess
import unicodedata
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]

from buddy_api.analysis.contracts import Manifest
from buddy_api.analysis.provider import ModelAction, ModelOutput
from buddy_api.extraction.contracts import Extraction, ExtractionError

MAX_PAGE_OBJECTS = 5000
MAX_TAG_NODES = 5000
MAX_TAG_DEPTH = 32
MAX_TEXT_CHARACTERS = 2_000_000
MAX_OCR_PAGES = 100
MAX_OCR_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_OCR_PIXELS = 16_777_216
OCR_TIMEOUT_SECONDS = 4
MIN_OCR_CONFIDENCE = 65.0

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float
    rect: Rect


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).replace("’", "'").casefold()
    return tuple(_WORD.findall(normalized))


def _clamp(rect: Rect, width: int, height: int) -> Rect | None:
    left, top = max(0.0, rect.x), max(0.0, rect.y)
    right, bottom = min(float(width), rect.right), min(float(height), rect.bottom)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def _from_page_points(
    rect: tuple[float, float, float, float], converter: object, width: int, height: int
) -> Rect | None:
    left, bottom, right, top = rect
    if not all(math.isfinite(value) for value in rect) or right <= left or top <= bottom:
        return None
    transform = converter.to_bitmap  # type: ignore[attr-defined]
    corners = (
        transform(left, bottom),
        transform(left, top),
        transform(right, bottom),
        transform(right, top),
    )
    xs = [float(point[0]) for point in corners]
    ys = [float(point[1]) for point in corners]
    return _clamp(Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)), width, height)


def _union(rects: Iterable[Rect]) -> Rect | None:
    items = tuple(rects)
    if not items:
        return None
    left, top = min(r.x for r in items), min(r.y for r in items)
    right, bottom = max(r.right for r in items), max(r.bottom for r in items)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def _same_visual_line(rects: Iterable[Rect]) -> bool:
    """Reject rectangles spanning multiple text baselines, including rotated lines."""
    items = tuple(rects)
    if not items:
        return False
    center_x = [rect.x + rect.width / 2 for rect in items]
    center_y = [rect.y + rect.height / 2 for rect in items]
    median_width = sorted(rect.width for rect in items)[len(items) // 2]
    median_height = sorted(rect.height for rect in items)[len(items) // 2]
    horizontal = max(center_y) - min(center_y) <= max(2.0, median_height * 0.65)
    vertical = max(center_x) - min(center_x) <= max(2.0, median_width * 0.65)
    return horizontal or vertical


def _matches_text(
    page: object,
    textpage: object,
    quote: str,
    width: int,
    height: int,
    converter: object,
) -> list[Rect]:
    matches: list[Rect] = []
    with closing(textpage.search(quote, match_case=False)) as searcher:  # type: ignore[attr-defined]
        while (match := searcher.get_next()) is not None:
            start, count = match
            boxes = []
            for index in range(start, start + count):
                char = textpage.get_text_range(index, 1)  # type: ignore[attr-defined]
                if not char or char.isspace():
                    continue
                box = textpage.get_charbox(index)  # type: ignore[attr-defined]
                converted = _from_page_points(box, converter, width, height)
                if converted is not None and converted.area > 0:
                    boxes.append(converted)
            if (rect := _union(boxes)) is not None and _same_visual_line(boxes):
                matches.append(rect)
            if len(matches) > 16:
                break
    if not matches:
        source_text = textpage.get_text_bounded()  # type: ignore[attr-defined]
        normalized_source = unicodedata.normalize("NFKC", source_text).casefold()
        normalized_quote = unicodedata.normalize("NFKC", quote).casefold()
        if normalized_quote and (
            normalized_quote != quote.casefold() or normalized_source != source_text.casefold()
        ):
            matches = _matches_normalized_text(
                textpage, source_text, normalized_quote, width, height, converter
            )
    return matches


def _matches_normalized_text(
    textpage: object,
    source_text: str,
    normalized_quote: str,
    width: int,
    height: int,
    converter: object,
) -> list[Rect]:
    """Map compatibility-normalized text back to its original source glyphs.

    NFKC can expand a ligature into several characters or combine a base character
    with a mark. Every normalized output character therefore maps back to the full
    source cluster that produced it; emitted geometry still comes only from source
    glyph boxes.
    """
    char_count = textpage.count_chars()  # type: ignore[attr-defined]
    source_chars = list(source_text)
    if len(source_chars) != char_count:
        return []
    normalized_chars: list[str] = []
    source_indices: list[tuple[int, ...]] = []
    index = 0
    while index < len(source_chars):
        cluster_end = index + 1
        while cluster_end < len(source_chars) and unicodedata.combining(source_chars[cluster_end]):
            cluster_end += 1
        cluster = "".join(source_chars[index:cluster_end])
        normalized_cluster = unicodedata.normalize("NFKC", cluster).casefold()
        normalized_chars.extend(normalized_cluster)
        source_indices.extend([(index, cluster_end)] * len(normalized_cluster))
        index = cluster_end

    normalized_text = "".join(normalized_chars)
    if normalized_text != unicodedata.normalize("NFKC", source_text).casefold():
        # Defensive consistency check: any unexpected PDFium/source-text mapping
        # prevents guessing at glyph positions.
        return []
    matches: list[Rect] = []
    start = 0
    while (start := normalized_text.find(normalized_quote, start)) >= 0:
        stop = start + len(normalized_quote)
        source_range = source_indices[start:stop]
        glyph_indices = sorted(
            {source_index for first, end in source_range for source_index in range(first, end)}
        )
        boxes = []
        for glyph_index in glyph_indices:
            if glyph_index >= len(source_chars) or source_chars[glyph_index].isspace():
                continue
            converted = _from_page_points(
                textpage.get_charbox(glyph_index),  # type: ignore[attr-defined]
                converter,
                width,
                height,
            )
            if converted is not None and converted.area > 0:
                boxes.append(converted)
        if (rect := _union(boxes)) is not None and _same_visual_line(boxes):
            matches.append(rect)
        if len(matches) > 16:
            break
        start += max(1, len(normalized_quote))
    return matches


def _adjacent_on_visual_line(previous: Rect, candidate: Rect) -> bool:
    """Return whether two word boxes are adjacent on one horizontal or vertical line."""
    vertical_overlap = max(
        0.0, min(previous.bottom, candidate.bottom) - max(previous.y, candidate.y)
    )
    horizontal_overlap = max(
        0.0, min(previous.right, candidate.right) - max(previous.x, candidate.x)
    )
    horizontal_line = (
        vertical_overlap >= min(previous.height, candidate.height) * 0.5
        and candidate.x >= previous.x
        and candidate.x - previous.right <= max(previous.height, candidate.height) * 2.0
    )
    vertical_line = (
        previous.height > previous.width * 1.25
        and candidate.height > candidate.width * 1.25
        and horizontal_overlap >= min(previous.width, candidate.width) * 0.5
        and candidate.y >= previous.y
        and candidate.y - previous.bottom <= max(previous.width, candidate.width) * 2.0
    )
    return horizontal_line or vertical_line


def _matches_text_words(
    page: object,
    textpage: object,
    quote: str,
    width: int,
    height: int,
    converter: object,
) -> list[Rect]:
    """Match exact words geometrically when PDF text blocks break phrase search.

    PDF producers often store visually adjacent words in separate text objects, so
    PDFium's phrase search cannot find the full quote. Word search preserves glyph
    geometry; this fallback only joins consecutive words that share a visual line.
    It deliberately declines wrapped phrases rather than drawing a paragraph box.
    """
    tokens = tuple(token for token in _tokens(quote) if any(char.isalnum() for char in token))
    if not 2 <= len(tokens) <= 32:
        return []
    word_matches: list[list[Rect]] = []
    for token in tokens:
        matches = _matches_text(page, textpage, token, width, height, converter)
        # Repeated/common tokens can create an unbounded combinatorial search. If
        # the page itself does not disambiguate them geometrically, abstain.
        if not matches or len(matches) > 64:
            return []
        word_matches.append(matches)
    paths: list[tuple[Rect, ...]] = [(rect,) for rect in word_matches[0]]
    for candidates in word_matches[1:]:
        next_paths = [
            (*path, candidate)
            for path in paths
            for candidate in candidates
            if _adjacent_on_visual_line(path[-1], candidate)
        ]
        if not next_paths or len(next_paths) > 256:
            return []
        paths = next_paths
    output = []
    for path in paths:
        if (rect := _union(path)) is not None:
            output.append(rect)
        if len(output) > 16:
            return []
    return output


def _ocr_words(page: object, width: int, height: int) -> list[OcrWord]:
    page_width, page_height = page.get_size()  # type: ignore[attr-defined]
    scale = min(3.0, math.sqrt(MAX_OCR_PIXELS / (page_width * page_height)))
    scale = max(1.0, scale)
    bitmap = page.render(  # type: ignore[attr-defined]
        scale=scale,
        may_draw_forms=False,
        force_bitmap_format=raw.FPDFBitmap_BGRA,
    )
    bitmap_width, bitmap_height = bitmap.width, bitmap.height
    try:
        rendered = bitmap.to_pil()
        image = rendered.convert("RGB")
        rendered.close()
        stream = io.BytesIO()
        try:
            image.save(stream, format="PNG", optimize=False)
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"],
                input=stream.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=OCR_TIMEOUT_SECONDS,
                check=False,
            )
        finally:
            image.close()
            stream.close()
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    finally:
        bitmap.close()
    if result.returncode != 0 or len(result.stdout) > MAX_OCR_OUTPUT_BYTES:
        return []
    try:
        rows = csv.DictReader(
            io.StringIO(result.stdout.decode("utf-8", errors="replace")), delimiter="\t"
        )
        scale_x, scale_y = width / bitmap_width, height / bitmap_height
        words: list[OcrWord] = []
        for row in rows:
            if row.get("level") != "5" or not (text := (row.get("text") or "").strip()):
                continue
            try:
                confidence = float(row["conf"])
                x, y = int(row["left"]), int(row["top"])
                w, h = int(row["width"]), int(row["height"])
            except (KeyError, TypeError, ValueError):
                continue
            if confidence < MIN_OCR_CONFIDENCE or w < 1 or h < 1:
                continue
            words.append(
                OcrWord(text, confidence, Rect(x * scale_x, y * scale_y, w * scale_x, h * scale_y))
            )
        return words
    except (csv.Error, UnicodeError):
        return []


def _match_ocr(words: list[OcrWord], quote: str) -> list[Rect]:
    target = _tokens(quote)
    if not target:
        return []
    tokens = [_tokens(word.text) for word in words]
    matches = []
    for start in range(len(tokens)):
        flat: list[str] = []
        end = start
        while end < len(tokens) and len(flat) < len(target):
            flat.extend(tokens[end])
            end += 1
        if tuple(flat) == target:
            selected = words[start:end]
            selected_rects = [word.rect for word in selected]
            if (
                selected
                and all(word.confidence >= MIN_OCR_CONFIDENCE for word in selected)
                and _same_visual_line(selected_rects)
                and (rect := _union(selected_rects)) is not None
            ):
                matches.append(rect)
    return matches[:17]


def _choose_match(matches: list[Rect], hint: Rect, width: int, height: int) -> Rect | None:
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    hx, hy = hint.center
    diagonal = math.hypot(width, height)
    ranked = sorted(
        (math.hypot(rect.center[0] - hx, rect.center[1] - hy), rect) for rect in matches
    )
    # Model geometry may disambiguate repeated exact phrases, but cannot pull the
    # match far from the source region or decide between nearly tied occurrences.
    if ranked[0][0] <= diagonal * 0.12 and ranked[1][0] - ranked[0][0] >= diagonal * 0.025:
        return ranked[0][1]
    return None


def _hint(action: ModelAction, width: int, height: int) -> Rect | None:
    if not all(math.isfinite(v) for v in (action.x, action.y, action.width, action.height)):
        return None
    return _clamp(Rect(action.x, action.y, action.width, action.height), width, height)


def _padded(rect: Rect, width: int, height: int, *, zoom: bool) -> Rect:
    if zoom:
        pad_x, pad_y = max(12.0, rect.width * 0.20), max(12.0, rect.height * 0.45)
    else:
        pad_x, pad_y = 1.5, 1.5
    return (
        _clamp(
            Rect(rect.x - pad_x, rect.y - pad_y, rect.width + 2 * pad_x, rect.height + 2 * pad_y),
            width,
            height,
        )
        or rect
    )


def _tagged_content_ids(page: object) -> set[int]:
    """Read bounded PDF structure-tree MCIDs; malformed tags are only ignored hints."""
    tree = raw.FPDF_StructTree_GetForPage(page)
    if not tree:
        return set()
    try:
        root_count = raw.FPDF_StructTree_CountChildren(tree)
        if not 0 <= root_count <= MAX_TAG_NODES:
            return set()
        pending = [
            (raw.FPDF_StructTree_GetChildAtIndex(tree, index), 1) for index in range(root_count)
        ]
        ids: set[int] = set()
        visited = 0
        while pending:
            element, depth = pending.pop()
            if not element:
                continue
            visited += 1
            if visited > MAX_TAG_NODES or depth > MAX_TAG_DEPTH:
                return set()
            count = raw.FPDF_StructElement_GetMarkedContentIdCount(element)
            if count > MAX_TAG_NODES:
                return set()
            if count > 0:
                for index in range(count):
                    mcid = raw.FPDF_StructElement_GetMarkedContentIdAtIndex(element, index)
                    if mcid >= 0:
                        ids.add(mcid)
            child_count = raw.FPDF_StructElement_CountChildren(element)
            if not 0 <= child_count <= MAX_TAG_NODES:
                return set()
            for index in range(child_count):
                child = raw.FPDF_StructElement_GetChildAtIndex(element, index)
                if child:
                    pending.append((child, depth + 1))
                else:
                    mcid = raw.FPDF_StructElement_GetChildMarkedContentID(element, index)
                    if mcid >= 0:
                        ids.add(mcid)
                if len(ids) > MAX_TAG_NODES:
                    return set()
        return ids
    except Exception:
        # Tagged-PDF support is experimental in the bundled PDFium API. A malformed
        # tree cannot be allowed to prevent valid source glyph/object geometry use.
        return set()
    finally:
        raw.FPDF_StructTree_Close(tree)


def _object_rects(
    page: object, converter: object, width: int, height: int
) -> tuple[list[tuple[int, Rect]], list[tuple[int, Rect]]]:
    output = []
    tagged = []
    tagged_ids = _tagged_content_ids(page)
    for index, obj in enumerate(page.get_objects()):  # type: ignore[attr-defined]
        if index >= MAX_PAGE_OBJECTS:
            return [], []
        if obj.type not in {
            raw.FPDF_PAGEOBJ_IMAGE,
            raw.FPDF_PAGEOBJ_PATH,
            raw.FPDF_PAGEOBJ_SHADING,
        }:
            continue
        rect = _from_page_points(obj.get_bounds(), converter, width, height)
        if rect is None:
            continue
        # Degenerate decorations are not useful targets. Keep large images available
        # for recognizing an internal crop, but never offer the whole page as a zoom.
        page_area = width * height
        if rect.area < page_area * 0.00002:
            continue
        output.append((obj.type, rect))
        if tagged_ids and raw.FPDFPageObj_GetMarkedContentID(obj.raw) in tagged_ids:
            tagged.append((obj.type, rect))
    return output, tagged


def _intersects(a: Rect, b: Rect) -> float:
    width = max(0.0, min(a.right, b.right) - max(a.x, b.x))
    height = max(0.0, min(a.bottom, b.bottom) - max(a.y, b.y))
    return width * height


def _snap_object(
    hint: Rect,
    objects: list[tuple[int, Rect]],
    width: int,
    height: int,
    tagged_objects: list[tuple[int, Rect]] | None = None,
) -> Rect | None:
    if tagged_objects:
        tagged_match = _snap_object_from_objects(hint, tagged_objects, width, height)
        if tagged_match is not None:
            return tagged_match
    return _snap_object_from_objects(hint, objects, width, height)


def _ground_object_target(
    hint: Rect,
    objects: list[tuple[int, Rect]],
    width: int,
    height: int,
    tagged_objects: list[tuple[int, Rect]] | None = None,
) -> tuple[Rect, bool] | None:
    """Snap whole-object targets; preserve internal image crops as QA-only candidates."""
    image_kind = raw.FPDF_PAGEOBJ_IMAGE
    all_containing = [
        rect
        for kind, rect in objects
        if kind == image_kind
        and rect.area > hint.area
        and _intersects(hint, rect) / hint.area >= 0.90
    ]
    tagged_containing = [
        rect
        for kind, rect in (tagged_objects or [])
        if kind == image_kind
        and rect.area > hint.area
        and _intersects(hint, rect) / hint.area >= 0.90
    ]
    # Tags are an optional disambiguation hint, never assumed to enumerate all page
    # objects. Fall back to the complete geometry inventory when tags are incomplete.
    containing = tagged_containing if len(tagged_containing) == 1 else all_containing
    if len(containing) == 1:
        image = containing[0]
        # A region that closely describes the complete source image is grounded by its
        # actual object bounds. A smaller crop is only a proposal: pixels within a
        # flattened image have no PDF object geometry, so keep it non-usable pending #50.
        if _intersects(hint, image) / image.area >= 0.90:
            if image.area > width * height * 0.78:
                return None
            return image, False
        return hint, True
    if len(containing) > 1:
        return None
    snapped = _snap_object(hint, objects, width, height, tagged_objects)
    if snapped is None:
        return None
    return snapped, False


def _snap_object_from_objects(
    hint: Rect, objects: list[tuple[int, Rect]], width: int, height: int
) -> Rect | None:
    page_area = width * height
    images = [
        rect
        for kind, rect in objects
        if kind == raw.FPDF_PAGEOBJ_IMAGE and rect.area <= page_area * 0.78
    ]
    ranked = sorted(
        ((_intersects(hint, rect) / rect.area, rect) for rect in images if rect.area > 0),
        key=lambda item: item[0],
        reverse=True,
    )
    if (
        ranked
        and ranked[0][0] >= 0.25
        and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.12)
    ):
        return ranked[0][1]
    if len(ranked) > 1 and ranked[0][0] >= 0.25:
        return None

    # Vector charts/diagrams contain many PDF paths rather than a single image. Keep
    # only spatially connected source objects that the coarse hint actually touches.
    paths = [
        rect for kind, rect in objects if kind in {raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING}
    ]
    touched = [rect for rect in paths if _intersects(hint, rect) / rect.area >= 0.35]
    if len(touched) < 3 or len(touched) > 250:
        return None
    diagonal = math.hypot(width, height)
    hx, hy = hint.center
    groups: list[list[Rect]] = []
    remaining = set(range(len(touched)))
    while remaining:
        seed = remaining.pop()
        group, frontier = {seed}, [seed]
        while frontier:
            current = frontier.pop()
            a = touched[current]
            for candidate in tuple(remaining):
                b = touched[candidate]
                dx = max(0.0, max(a.x - b.right, b.x - a.right))
                dy = max(0.0, max(a.y - b.bottom, b.y - a.bottom))
                if math.hypot(dx, dy) <= max(10.0, diagonal * 0.018):
                    remaining.remove(candidate)
                    group.add(candidate)
                    frontier.append(candidate)
        if len(group) >= 3:
            groups.append([touched[index] for index in group])
    ranked_groups: list[tuple[float, Rect]] = []
    for rect_group in groups:
        merged_rect = _union(rect_group)
        if merged_rect is not None:
            ranked_groups.append(
                (
                    math.hypot(merged_rect.center[0] - hx, merged_rect.center[1] - hy),
                    merged_rect,
                )
            )
    ranked_groups.sort(key=lambda item: item[0])
    if not ranked_groups or ranked_groups[0][0] > diagonal * 0.08:
        return None
    if len(ranked_groups) > 1 and ranked_groups[1][0] - ranked_groups[0][0] < diagonal * 0.025:
        return None
    region = ranked_groups[0][1]
    if region.area > width * height * 0.65:
        return None
    return region


def ground_actions(
    content: bytes, extraction: Extraction, manifest: Manifest, output: ModelOutput
) -> ModelOutput:
    """Replace untrusted coordinates with exact source geometry, dropping ambiguity."""
    if hashlib.sha256(content).hexdigest() != extraction.source_sha256:
        raise ExtractionError("source_mismatch")
    if (
        not 1 <= len(extraction.slides) <= 100
        or len(output.slides) != len(extraction.slides)
        or len(manifest.slides) != len(extraction.slides)
        or [slide.number for slide in output.slides] != list(range(1, len(extraction.slides) + 1))
        or sum(len(slide.actions) for slide in output.slides) > 40
    ):
        raise ExtractionError("grounding_page_limit")
    grounded: list[object] = []
    ocr_cache: dict[int, list[OcrWord]] = {}
    ocr_pages = 0
    total_chars = 0

    try:
        with pdfium.PdfDocument(content) as document:
            if len(document) != len(extraction.slides):
                raise ExtractionError("page_count_mismatch")
            for page_index, (_slide, generated) in enumerate(
                zip(extraction.slides, output.slides, strict=True)
            ):
                page_actions = []
                if not generated.actions:
                    grounded.append(generated)
                    continue
                width, height = (
                    manifest.slides[page_index].source.width,
                    manifest.slides[page_index].source.height,
                )
                with (
                    closing(document[page_index]) as page,
                    closing(page.get_textpage()) as textpage,
                ):
                    total_chars += textpage.count_chars()
                    if total_chars > MAX_TEXT_CHARACTERS:
                        raise ExtractionError("grounding_text_limit")
                    text = textpage.get_text_bounded()
                    if len(text) > MAX_TEXT_CHARACTERS:
                        raise ExtractionError("grounding_text_limit")
                    # PDFium's transform includes crop, rotation and display origin. Its
                    # 144-dpi bitmap dimensions must match the persisted action plane.
                    with closing(
                        page.render(
                            scale=2,
                            may_draw_forms=False,
                            force_bitmap_format=raw.FPDFBitmap_BGRA,
                        )
                    ) as bitmap:
                        if (bitmap.width, bitmap.height) != (width, height):
                            raise ExtractionError("page_geometry_mismatch")
                        converter = bitmap.get_posconv(page)
                        objects, tagged_objects = _object_rects(page, converter, width, height)
                        for action in generated.actions:
                            hint = _hint(action, width, height)
                            if hint is None:
                                continue
                            rect: Rect | None = None
                            if action.target_text:
                                matches = _matches_text(
                                    page, textpage, action.target_text, width, height, converter
                                )
                                if not matches:
                                    matches = _matches_text_words(
                                        page,
                                        textpage,
                                        action.target_text,
                                        width,
                                        height,
                                        converter,
                                    )
                                if not matches:
                                    if page_index not in ocr_cache and ocr_pages < MAX_OCR_PAGES:
                                        ocr_cache[page_index] = _ocr_words(page, width, height)
                                        ocr_pages += 1
                                    matches = _match_ocr(
                                        ocr_cache.get(page_index, []), action.target_text
                                    )
                                rect = _choose_match(matches, hint, width, height)
                                if rect is not None:
                                    rect = _padded(
                                        rect,
                                        width,
                                        height,
                                        zoom=action.type == "zoom_region",
                                    )
                            elif action.type == "zoom_region":
                                grounded_target = _ground_object_target(
                                    hint, objects, width, height, tagged_objects=tagged_objects
                                )
                                if grounded_target is not None:
                                    rect, qa_pending = grounded_target
                                    if qa_pending:
                                        action = action.model_copy(
                                            update={"visual_qa_pending": True}
                                        )
                            if rect is not None:
                                page_actions.append(
                                    action.model_copy(
                                        update={
                                            "x": rect.x,
                                            "y": rect.y,
                                            "width": rect.width,
                                            "height": rect.height,
                                        }
                                    )
                                )
                grounded.append(generated.model_copy(update={"actions": tuple(page_actions)}))
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError("pdf_action_grounding_failed") from None
    return output.model_copy(update={"slides": tuple(grounded)})
