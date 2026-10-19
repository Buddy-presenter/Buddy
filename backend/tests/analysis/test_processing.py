"""Bind model suggestions to Buddy slide IDs and immutable validation inputs."""

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from buddy_api.analysis.provider import (
    ModelAction,
    ModelOutput,
    ModelSlide,
    ModelSummary,
    ProviderFailure,
)
from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.runner import extract_pdf
from buddy_api.processing.service import Processor

PDF = Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"


def test_model_output_is_bound_to_verified_manifest_and_stable_action_ids():
    content = PDF.read_bytes()
    deck_id, job_id, extraction_id = uuid4(), uuid4(), uuid4()
    extraction = extract_pdf(BytesIO(content), source_revision="fixture-revision")
    manifest = render_manifest(content, extraction, str(deck_id))
    generated = ModelOutput(
        presentation=ModelSummary(text="A short geometry presentation.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index + 1,
                text=f"Page {index + 1} introduces a geometry example.",
                reason=None,
                actions=(
                    ModelAction(
                        type="highlight_region",
                        purpose="Focus attention on the diagram.",
                        x=1,
                        y=1,
                        width=20,
                        height=20,
                    ),
                )
                if index == 0
                else (),
            )
            for index in range(len(extraction.slides))
        ),
    )

    first = Processor._analysis_document(
        generated, extraction, manifest, deck_id, job_id, extraction_id
    )
    second = Processor._analysis_document(
        generated, extraction, manifest, deck_id, job_id, extraction_id
    )

    assert first.presentation_summary.text == "A short geometry presentation."
    assert [slide.slide_id for slide in first.slides] == [page.id for page in extraction.slides]
    action = first.slides[0].actions[0]
    assert action.deck_id == str(deck_id)
    assert action.render_id == manifest.render_id
    assert action.slide_id == extraction.slides[0].id
    assert action.instance_id == second.slides[0].actions[0].instance_id


def test_model_action_outside_verified_page_is_rejected():
    content = PDF.read_bytes()
    deck_id, job_id, extraction_id = uuid4(), uuid4(), uuid4()
    extraction = extract_pdf(BytesIO(content), source_revision="fixture-revision")
    manifest = render_manifest(content, extraction, str(deck_id))
    generated = ModelOutput(
        presentation=ModelSummary(text="A short geometry presentation.", reason=None),
        slides=tuple(
            ModelSlide(
                number=index + 1,
                text=f"Page {index + 1} introduces a geometry example.",
                reason=None,
                actions=(
                    ModelAction(
                        type="zoom_region",
                        purpose="Focus on a region.",
                        x=0,
                        y=0,
                        width=manifest.slides[index].source.width + 1,
                        height=20,
                    ),
                )
                if index == 0
                else (),
            )
            for index in range(len(extraction.slides))
        ),
    )

    with pytest.raises(ProviderFailure, match="provider_invalid_analysis"):
        Processor._analysis_document(
            generated, extraction, manifest, deck_id, job_id, extraction_id
        )
