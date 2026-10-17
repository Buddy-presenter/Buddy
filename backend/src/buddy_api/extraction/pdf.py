"""Bounded PDF text extraction. No scripts, attachments, links or form actions execute."""

import ctypes
import hashlib
import math
import time
from contextlib import closing
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]

from buddy_api.extraction.contracts import (
    Bounds,
    Extraction,
    ExtractionError,
    Limits,
    Point,
    Slide,
    SlideObject,
    Warning,
)


def read_pdf(path: str, revision: str, limits: Limits) -> Extraction:
    start = time.monotonic()
    content = Path(path).read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ExtractionError("invalid_pdf")
    digest = hashlib.sha256(content).hexdigest()
    slides, warnings = [], []
    characters = objects = 0
    with pdfium.PdfDocument(content) as document:
        if raw.FPDF_GetSecurityHandlerRevision(document) != -1:
            raise ExtractionError("encrypted_pdf")
        if not 1 <= len(document) <= limits.slides:
            raise ExtractionError("slide_limit")
        for index in range(len(document)):
            if time.monotonic() - start > limits.seconds:
                raise ExtractionError("time_limit")
            slide_id = str(uuid5(NAMESPACE_URL, f"buddy:{digest}:{revision}:pdf:{index}"))
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                # Canonical pixels at 144 dpi; EMU remains the extraction coordinate plane.
                # Round once, then use the same plane for every text rectangle and renderer.
                width, height = (math.ceil(value * 2) for value in page.get_size())
                if not (1 <= width <= 8192 and 1 <= height <= 8192) or width * height > 16_777_216:
                    raise ExtractionError("page_size_limit")
                count = textpage.count_chars()
                characters += count
                if characters > limits.text_characters:
                    raise ExtractionError("text_limit")
                text = textpage.get_text_bounded().replace("\r\n", "\n")
                rectangles = textpage.count_rects()
                objects += rectangles
                if objects > limits.objects:
                    raise ExtractionError("object_limit")
                extracted = []

                def point(
                    x: float, y: float, width: int = width, height: int = height
                ) -> tuple[int, int]:
                    dx, dy = ctypes.c_int(), ctypes.c_int()
                    if not raw.FPDF_PageToDevice(
                        page, 0, 0, width, height, 0, x, y, ctypes.byref(dx), ctypes.byref(dy)
                    ):
                        raise ExtractionError("invalid_geometry")
                    return max(0, min(width, dx.value)) * 6350, max(0, min(height, dy.value)) * 6350

                for position in range(rectangles):
                    left, bottom, right, top = textpage.get_rect(position)
                    if not all(math.isfinite(v) for v in (left, bottom, right, top)):
                        raise ExtractionError("invalid_geometry")
                    points = [
                        point(x, y)
                        for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
                    ]
                    xs, ys = zip(*points, strict=True)
                    bounds = Bounds(
                        x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys)
                    )
                    extracted.append(
                        SlideObject(
                            id=f"{slide_id}:text:{position}",
                            source_id=position,
                            parent_id=None,
                            kind="shape",
                            text=textpage.get_text_bounded(left, bottom, right, top),
                            bounds=bounds,
                            corners=(
                                Point(x=points[0][0], y=points[0][1]),
                                Point(x=points[1][0], y=points[1][1]),
                                Point(x=points[2][0], y=points[2][1]),
                                Point(x=points[3][0], y=points[3][1]),
                            ),
                        )
                    )
                slides.append(
                    Slide(
                        id=slide_id,
                        source_id=index + 1,
                        index=index,
                        width=width * 6350,
                        height=height * 6350,
                        text=text,
                        notes=None,
                        objects=tuple(extracted),
                    )
                )
                warnings.append(Warning(code="notes_unavailable", slide_id=slide_id))
                if not text.strip():
                    warnings.append(Warning(code="text_unavailable_no_ocr", slide_id=slide_id))
    warnings.append(Warning(code="pdf_static_content_only"))
    return Extraction(
        source_revision=revision,
        source_sha256=digest,
        source_format="pdf",
        slides=tuple(slides),
        warnings=tuple(warnings),
    )
