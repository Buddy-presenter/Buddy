"""A session may only follow the exact saved PowerPoint and slide map."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
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


def test_present_syncs_verified_powerpoint_slide_and_rejects_foreign_show(
    desktop_window, monkeypatch
):
    analysis, content, info = example()
    binding = PowerPointSessionBinding.create(analysis, content, info)
    window = desktop_window()
    present = window.present
    present.set_account_identity(str(uuid4()), "access")
    monkeypatch.setattr(present.powerpoint, "release_zoom", lambda: None)
    connects = []
    monkeypatch.setattr(
        present, "_submit_powerpoint", lambda action, path: connects.append((action, path))
    )
    present.bind_saved_powerpoint(binding)
    assert connects == [("connect", binding.path)]
    monkeypatch.setattr(present, "_ensure_session", lambda **_kwargs: None)
    present._attach_powerpoint_info(info)
    assert present._current_local_state() == SessionState("slide-1")
    present._agent_proposed(
        {"target": {"tool": "go_to_slide", "slide_id": "slide-2"}}
    )
    assert present.agent_proposal.text() == "Suggested: Go to slide 2"
    present._session = PresentationSession(
        str(uuid4()), str(uuid4()), binding.deck_id, binding.analysis_id,
        "active", 1, SessionState("slide-1"), datetime.now(UTC)
    )
    next_info = replace(info, slide_index=2, slide_id=102)
    present._attach_powerpoint_info(next_info)
    assert present._session_pending_state == SessionState("slide-2")
    assert not present._microphone_session_available()
    present._attach_powerpoint_info(replace(next_info, window_id=99))
    assert present._powerpoint_binding is None
    assert present._session is None
    assert not present._microphone_session_available()
