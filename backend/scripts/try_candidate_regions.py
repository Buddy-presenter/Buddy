"""Offline text-ID experiment. Select a measured line; never invoke a paid model."""

import argparse
import json
from contextlib import closing
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pypdfium2 as pdfium
from PIL import ImageDraw

from buddy_api.analysis.provider import ModelAction, ModelOutput, ModelSlide, ModelSummary
from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.runner import extract_pdf
from buddy_api.processing.stage import execute_text_candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument(
        "--contains", required=True, help="Unique substring of a measured text line"
    )
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory; existing evidence is never overwritten.")
    if args.pdf.stat().st_size > 18 * 1024 * 1024:
        parser.error("PDF exceeds the bounded analysis input limit.")
    content = args.pdf.read_bytes()
    catalog = execute_text_candidates(content)
    matches = [
        target
        for target in catalog.candidates
        if target.page == args.page and args.contains.casefold() in target.text.casefold()
    ]
    if not args.contains.strip() or len(matches) != 1:
        parser.error("Select a substring matching exactly one candidate on the requested page.")
    target = matches[0]
    extraction = extract_pdf(BytesIO(content), source_revision="candidate-experiment")
    manifest = render_manifest(content, extraction, str(uuid4()))
    # Intentionally wrong coordinates demonstrate that source ID drives positioning.
    action = ModelAction(
        type="highlight_region",
        purpose="Offline investigator-selected text line.",
        target_text=target.text,
        target_id=target.id,
        x=0,
        y=0,
        width=1,
        height=1,
    )
    generated = ModelOutput(
        presentation=ModelSummary(text="Offline geometry experiment, no model call.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index + 1,
                text="Offline geometry experiment.",
                reason=None,
                actions=(action,) if index == args.page - 1 else (),
            )
            for index in range(len(extraction.slides))
        ),
    )
    # Use the same bounded child as the worker for final target-ID remeasurement.
    from buddy_api.processing.stage import execute_grounding

    result = execute_grounding(content, extraction, manifest, generated)
    resolved = result.slides[args.page - 1].actions[0]
    with (
        pdfium.PdfDocument(content) as document,
        closing(document[args.page - 1]) as page,
        closing(page.render(scale=2, may_draw_forms=False)) as bitmap,
        bitmap.to_pil() as source,
    ):
        image = source.convert("RGB")
    with image:
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            (resolved.x, resolved.y, resolved.x + resolved.width, resolved.y + resolved.height),
            outline="#009B70",
            width=5,
        )
        args.output.mkdir(parents=True)
        image.save(args.output / "selected-line.png")
    report = {
        "source_sha256": catalog.source_sha256,
        "model_called": False,
        "selection": "Investigator-selected candidate; not model-quality evidence.",
        "candidate": target.model_dump(mode="json"),
        "resolved_action": resolved.model_dump(mode="json"),
    }
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (args.output / "candidates.json").write_text(
        catalog.model_dump_json(indent=2), encoding="utf-8"
    )
    print("Saved private selected-line.png, result.json and candidates.json. No model was called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
