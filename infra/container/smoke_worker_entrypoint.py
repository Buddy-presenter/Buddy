"""Run the worker smoke with a deterministic response at the provider boundary."""

import hashlib
import os

from buddy_api.analysis.contracts import Manifest
from buddy_api.analysis.provider import BedrockProvider, ModelOutput, ModelSlide, ModelSummary
from buddy_api.extraction.contracts import Extraction
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


BedrockProvider.analyze_async = deterministic_analysis
raise SystemExit(main())
