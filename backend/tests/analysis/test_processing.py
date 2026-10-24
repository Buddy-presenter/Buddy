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
                        purpose="Highlight the visible phrase “Diagram A”.",
                        x=1,
                        y=1,
                        width=20,
                        height=20,
                    ),
                    ModelAction(
                        type="zoom_region",
                        purpose="Show the full diagram and its caption.",
                        x=1,
                        y=1,
                        width=40,
                        height=30,
                    ),
                    ModelAction(
                        type="highlight_region",
                        purpose="Highlight the visible phrase “Diagram A”.",
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
    assert len(first.slides[0].actions) == 3
    assert first.slides[0].actions[1].type == "zoom_region"
    assert first.slides[0].actions[0].instance_id != first.slides[0].actions[1].instance_id
    duplicate_ids = Processor._duplicate_candidate_ids(first.slides)
    assert duplicate_ids == {first.slides[0].actions[2].instance_id}


@pytest.mark.parametrize(
    ("action_type", "x", "y", "width", "height"),
    [("zoom_region", 0, 0, "page_width", 20), ("zoom_region", 0, 0, 1, 1)],
)
def test_unsafe_model_action_is_preserved_for_isolated_validation(action_type, x, y, width, height):
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
                        type=action_type,
                        purpose="Focus on a region.",
                        x=x,
                        y=y,
                        width=(
                            manifest.slides[index].source.width + 1
                            if width == "page_width"
                            else width
                        ),
                        height=height,
                    ),
                )
                if index == 0
                else (),
            )
            for index in range(len(extraction.slides))
        ),
    )

    document = Processor._analysis_document(
        generated, extraction, manifest, deck_id, job_id, extraction_id
    )

    assert document.presentation_summary.text == "A short geometry presentation."
    assert all(slide.summary.text for slide in document.slides)
    action = document.slides[0].actions[0]
    assert action.type == action_type
    assert action.params.region.x == x
    assert action.params.region.width == (
        manifest.slides[0].source.width + 1 if width == "page_width" else width
    )
    assert all(not slide.actions for slide in document.slides[1:])
