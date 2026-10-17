"""Private subprocess PDFium entry point. Never initialize forms or execute PDF actions."""

import json
import math
import sys
from contextlib import closing
from pathlib import Path


def main() -> int:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]
    import pypdfium2.raw as raw  # type: ignore[import-untyped]

    folder = Path(sys.argv[-1])
    try:
        request = json.loads((folder / "request.json").read_text())
        with pdfium.PdfDocument(folder / "source.pdf") as document:
            if raw.FPDF_GetSecurityHandlerRevision(document) != -1:
                raise ValueError
            if not 1 <= len(document) <= 500:
                raise ValueError
            sizes = []
            for index in range(len(document)):
                size = document.get_page_size(index)
                if not all(math.isfinite(v) and 0.5 <= v <= 4096 for v in size):
                    raise ValueError
                width, height = (math.ceil(v * 2) for v in size)
                if width * height > 16_777_216:
                    raise ValueError
                sizes.append([width, height])
            index = request.get("index", 0)
            width, height = sizes[index]
            # PDFium applies crop/rotation while painting. Fixed 144 dpi keeps
            # the same canonical plane as backend PDF extraction.
            with (
                closing(document[index]) as page,
                closing(
                    page.render(
                        scale=2, may_draw_forms=False, force_bitmap_format=raw.FPDFBitmap_BGRA
                    )
                ) as bitmap,
            ):
                if bitmap.width * bitmap.height > 16_777_216:
                    raise ValueError
                (folder / "pixels.bin").write_bytes(bytes(bitmap.buffer))
                (folder / "result.json").write_text(
                    json.dumps(
                        {
                            "sizes": sizes,
                            "width": bitmap.width,
                            "height": bitmap.height,
                            "stride": bitmap.stride,
                        }
                    )
                )
        return 0
    except Exception:
        # No parser messages or document content in diagnostics.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
