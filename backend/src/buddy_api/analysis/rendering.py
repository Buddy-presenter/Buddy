"""Create ephemeral PDFium raster digests for the #11 provenance manifest.

No page image is written, retained, or passed to the provider. The provider receives
only the accepted original PDF through its supported PDF input API.
"""

import hashlib
from contextlib import closing
from io import BytesIO

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as raw  # type: ignore[import-untyped]

from buddy_api.analysis.contracts import Manifest, ManifestSlide, Raster, SourcePlane
from buddy_api.extraction.contracts import Extraction, ExtractionError

RENDERER_VERSION = "pdfium-5.14.0-144dpi"
MAX_ANALYSIS_PAGES = 200


def render_manifest(content: bytes, extraction: Extraction, deck_id: str) -> Manifest:
    if hashlib.sha256(content).hexdigest() != extraction.source_sha256:
        raise ExtractionError("source_mismatch")
    if len(extraction.slides) > MAX_ANALYSIS_PAGES:
        raise ExtractionError("analysis_page_limit")
    render_id = RENDERER_VERSION + ":" + extraction.source_sha256
    slides: list[dict[str, object]] = []
    try:
        with pdfium.PdfDocument(content) as document:
            if len(document) != len(extraction.slides):
                raise ExtractionError("page_count_mismatch")
            for index, page_info in enumerate(extraction.slides):
                width, height = page_info.width // 6350, page_info.height // 6350
                with (
                    closing(document[index]) as page,
                    closing(
                        page.render(
                            scale=2,
                            may_draw_forms=False,
                            force_bitmap_format=raw.FPDFBitmap_BGRA,
                        )
                    ) as bitmap,
                ):
                    if (bitmap.width, bitmap.height) != (width, height):
                        raise ExtractionError("page_geometry_mismatch")
                    image = bitmap.to_pil()
                    buffer = BytesIO()
                    image.save(buffer, format="PNG", optimize=False)
                    digest = hashlib.sha256(buffer.getbuffer()).hexdigest()
                    image.close()
                    buffer.close()
                slides.append(
                    ManifestSlide(
                        slide_id=page_info.id,
                        source=SourcePlane(
                            width=width,
                            height=height,
                            units="source_pixels",
                            origin="top_left",
                        ),
                        raster=Raster(
                            path=f"{page_info.id}.png",
                            width=width,
                            height=height,
                            transform="scale",
                            sha256=digest,
                        ),
                    ).model_dump(mode="json")
                )
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError("pdf_render_manifest_failed") from None
    return Manifest.model_validate(
        {"version": "1.0", "deck_id": deck_id, "render_id": render_id, "slides": slides}
    )
