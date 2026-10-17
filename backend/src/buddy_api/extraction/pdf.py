"""Inspect PDF page identity/dimensions only; do not extract document content."""

import hashlib
import math
import time
from contextlib import closing
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]

from buddy_api.extraction.contracts import Extraction, ExtractionError, Limits, Slide, Warning


def read_pdf(path: str, revision: str, limits: Limits) -> Extraction:
    start = time.monotonic()
    content = Path(path).read_bytes()
    if not content.startswith(b"%PDF-"):
        raise ExtractionError("invalid_pdf")
    digest = hashlib.sha256(content).hexdigest()
    slides = []
    with pdfium.PdfDocument(content) as document:
        if raw.FPDF_GetSecurityHandlerRevision(document) != -1:
            raise ExtractionError("encrypted_pdf")
        if not 1 <= len(document) <= limits.slides:
            raise ExtractionError("slide_limit")
        for index in range(len(document)):
            if time.monotonic() - start > limits.seconds:
                raise ExtractionError("time_limit")
            slide_id = str(uuid5(NAMESPACE_URL, f"buddy:{digest}:{revision}:pdf:{index}"))
            with closing(document[index]) as page:
                # Page dimensions match the viewer's 144-dpi coordinate plane.
                width, height = (math.ceil(value * 2) for value in page.get_size())
                if not (1 <= width <= 8192 and 1 <= height <= 8192) or width * height > 16_777_216:
                    raise ExtractionError("page_size_limit")
                slides.append(
                    Slide(
                        id=slide_id,
                        source_id=index + 1,
                        index=index,
                        width=width * 6350,
                        height=height * 6350,
                        text="",
                        notes=None,
                        objects=(),
                    )
                )
    return Extraction(
        source_revision=revision,
        source_sha256=digest,
        source_format="pdf",
        slides=tuple(slides),
        warnings=(Warning(code="pdf_content_not_extracted"),),
    )
