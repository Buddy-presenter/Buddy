"""A session may only follow the exact saved PowerPoint and slide map."""

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.review_service import ReviewAnalysis, ReviewSlide, ReviewSummary


def example():
    content = b"private saved source"
    summary = ReviewSummary("available", "Example", None)
    slides = (
        ReviewSlide("slide-1", 960, 540, summary, (), native_slide_id=101),
        ReviewSlide("slide-2", 960, 540, summary, (), native_slide_id=102),
    )
    analysis = ReviewAnalysis(
        uuid4(),
        uuid4(),
        1,
        "published",
        False,
        "revision-1",
        hashlib.sha256(content).hexdigest(),
        summary,
        slides,
        "pptx",
        "render-1",
        True,
    )
    info = PresentationInfo("C:/slides/deck.pptx", "deck.pptx", 1, 2, 42, 101, 960, 540)
    return analysis, content, info


def test_binding_tracks_matching_native_slide_ids():
    analysis, content, info = example()
    binding = PowerPointSessionBinding.create(analysis, content, info)
    assert binding.slide_id(info) == "slide-1"
    assert binding.slide_id(replace(info, slide_index=2, slide_id=102)) == "slide-2"


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "C:/slides/other.pptx"},
        {"window_id": 43},
        {"slide_count": 3},
        {"slide_id": 102},
        {"slide_width": 800},
        {"slide_index": 0},
    ],
)
def test_binding_rejects_changed_slideshow_identity(changes):
    analysis, content, info = example()
    binding = PowerPointSessionBinding.create(analysis, content, info)
    with pytest.raises(PowerPointError):
        binding.slide_id(replace(info, **changes))


def test_binding_refuses_different_source_bytes():
    analysis, _, info = example()
    with pytest.raises(PowerPointError):
        PowerPointSessionBinding.create(analysis, b"changed source", info)
