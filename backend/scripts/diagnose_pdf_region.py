"""Compare a saved highlight with current source grounding, without calling a model.

Outputs contain private document content. Keep the output directory outside Git.
"""

import argparse
import hashlib
import json
import math
from contextlib import closing
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pypdfium2 as pdfium
from PIL import ImageDraw

from buddy_api.analysis.provider import ModelAction, ModelOutput, ModelSlide, ModelSummary
from buddy_api.analysis.rendering import RENDERER_VERSION, render_manifest
from buddy_api.extraction.runner import extract_pdf
from buddy_api.processing.stage import execute_grounding


def diagnose(
    content: bytes,
    page_number: int,
    target: str,
    saved_region: tuple[float, float, float, float] | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """A unique source match must remain correct even when the saved hint is wrong."""
    source_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        raise ValueError("Source SHA-256 differs from the saved analysis; comparison refused.")
    if not target.strip() or len(target) > 256:
        raise ValueError("Supply one exact visible line or phrase of at most 256 characters.")
    extraction = extract_pdf(BytesIO(content), source_revision="local-region-diagnostic")
    if not 1 <= page_number <= len(extraction.slides):
        raise ValueError("Page is outside the source PDF.")
    manifest = render_manifest(content, extraction, str(uuid4()))
    page = manifest.slides[page_number - 1].source
    hint = saved_region or (0.0, 0.0, float(page.width), float(page.height))
    x, y, width, height = hint
    if (
        not all(math.isfinite(value) for value in hint)
        or min(x, y) < 0
        or min(width, height) <= 0
        or x + width > page.width
        or y + height > page.height
    ):
        raise ValueError("Saved region must be finite and inside the canonical source page.")
    candidate = ModelAction(
        type="highlight_region",
        purpose="Locate the supplied exact visible target for diagnosis.",
        target_text=target,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    generated = ModelOutput(
        presentation=ModelSummary(text="Local geometry diagnostic.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index + 1,
                text="Local geometry diagnostic.",
                reason=None,
                actions=(candidate,) if index == page_number - 1 else (),
            )
            for index in range(len(extraction.slides))
        ),
    )
    result = execute_grounding(content, extraction, manifest, generated, seconds=120)
    actions = result.slides[page_number - 1].actions
    grounded = (
        {key: getattr(actions[0], key) for key in ("x", "y", "width", "height")}
        if actions
        else None
    )
    overlap = None
    if grounded is not None and saved_region is not None:
        left, top = max(x, grounded["x"]), max(y, grounded["y"])
        right = min(x + width, grounded["x"] + grounded["width"])
        bottom = min(y + height, grounded["y"] + grounded["height"])
        intersection = max(0, right - left) * max(0, bottom - top)
        overlap = intersection / (
            width * height + grounded["width"] * grounded["height"] - intersection
        )
    return {
        "schema_version": "buddy.region-diagnostic.v1",
        "source_sha256": source_sha256,
        "source_identity_verified": expected_sha256 is not None,
        "renderer_version": RENDERER_VERSION,
        "page_number": page_number,
        "page_size": {"width": page.width, "height": page.height},
        "target_text": target,
        "saved_region": dict(zip(("x", "y", "width", "height"), saved_region, strict=True))
        if saved_region is not None
        else None,
        "grounded_region": grounded,
        "grounding_trace": actions[0].grounding_trace.model_dump(mode="json")
        if actions and actions[0].grounding_trace is not None
        else None,
        "intersection_over_union": overlap,
        "semantic_verified": False,
        "note": "Geometry comparison only. No model call, semantic approval or persisted changes.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, required=True, help="One-based PDF page number")
    parser.add_argument("--target", required=True, help="Exact visible single-line text")
    parser.add_argument("--saved-region", nargs=4, type=float, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--source-sha256", help="Source digest from the saved analysis")
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    args = parser.parse_args()
    if args.pdf.stat().st_size > 18 * 1024 * 1024:
        parser.error("Source exceeds the analysis input limit (18 MiB).")
    if args.output.exists():
        parser.error("Choose a new output directory to preserve earlier diagnostic evidence.")
    content = args.pdf.read_bytes()
    try:
        report = diagnose(
            content,
            args.page,
            args.target,
            tuple(args.saved_region) if args.saved_region is not None else None,
            args.source_sha256,
        )
    except ValueError as exc:
        parser.error(str(exc))
    with (
        pdfium.PdfDocument(content) as document,
        closing(document[args.page - 1]) as page,
        closing(page.render(scale=2, may_draw_forms=False)) as bitmap,
        bitmap.to_pil() as source,
    ):
        image = source.convert("RGB")
    with image:
        draw = ImageDraw.Draw(image)
        for key, color in (("saved_region", "#FF7800"), ("grounded_region", "#009B70")):
            region = report[key]
            if region is not None:
                x, y, width, height = (region[name] for name in ("x", "y", "width", "height"))
                draw.rectangle((x, y, x + width, y + height), outline=color, width=4)
        args.output.mkdir(parents=True)
        image.save(args.output / "comparison.png")
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Saved private report.json and comparison.png (orange=saved, green=current grounding).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
