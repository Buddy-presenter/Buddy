"""Run the worker smoke with a deterministic response at the provider boundary."""

import hashlib
import io
import os
from pathlib import Path
from uuid import uuid4

from buddy_api.analysis.contracts import Manifest
from buddy_api.analysis.grounding import ground_actions
from buddy_api.analysis.provider import (
    BedrockProvider,
    ModelAction,
    ModelOutput,
    ModelSlide,
    ModelSummary,
)
from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.contracts import Extraction
from buddy_api.extraction.runner import extract_pdf
from buddy_api.processing.__main__ import main


async def deterministic_analysis(
    self: BedrockProvider,
    content: bytes,
    extraction: Extraction,
    deck_id: str,
    manifest: Manifest,
) -> ModelOutput:
    expected = os.environ["BUDDY_SMOKE_PDF_SHA256"]
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected or extraction.source_sha256 != expected:
        raise RuntimeError("Worker did not submit the accepted original PDF bytes.")
    return ModelOutput(
        presentation=ModelSummary(text="Deterministic worker smoke response.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index,
                text=f"Deterministic smoke response for page {index}.",
                reason=None,
                actions=(),
            )
            for index, _page in enumerate(extraction.slides, start=1)
        ),
    )


def verify_scanned_grounding() -> None:
    content = Path("/scanned-source-anchor.pdf").read_bytes()
    extraction = extract_pdf(io.BytesIO(content), source_revision="scan-smoke")
    manifest = render_manifest(content, extraction, str(uuid4()))
    if extraction.slides[0].text:
        raise RuntimeError("OCR fixture unexpectedly has a native PDF text layer")
    action = ModelAction(
        type="highlight_region",
        purpose="Highlight the exact phrase Revenue increased 15 percent.",
        target_text="Revenue increased 15 percent",
        x=0,
        y=0,
        width=manifest.slides[0].source.width,
        height=manifest.slides[0].source.height,
    )
    generated = ModelOutput(
        presentation=ModelSummary(text="Scanned page.", reason=None),
        slides=(ModelSlide(number=1, text="Scanned result.", reason=None, actions=(action,)),),
    )
    result = ground_actions(content, extraction, manifest, generated)
    if len(result.slides[0].actions) != 1:
        raise RuntimeError("Bounded OCR did not resolve the scanned phrase")
    grounded = result.slides[0].actions[0]
    if (
        grounded.width >= manifest.slides[0].source.width * 0.8
        or grounded.height >= manifest.slides[0].source.height * 0.08
    ):
        raise RuntimeError("OCR grounding produced a non-precise highlight region")


BedrockProvider.analyze_async = deterministic_analysis
verify_scanned_grounding()
raise SystemExit(main())
