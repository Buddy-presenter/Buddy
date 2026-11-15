"""Capture actual PDF/native player completion and feed the production replay scorer."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from frontend.tests.test_transition_player import native_player as native_player
from frontend.tests.test_transition_player import player as player

from buddy_api.replay import Corpus, Gates, Trace, score
from buddy_desktop.transition_capture import TransitionCapture
from buddy_desktop.transition_policy import MODEL, VERSION


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
def test_capture_uses_player_completion_not_proposals(request, qtbot, kind):
    fixture = request.getfixturevalue("native_player" if kind == "pptx" else "player")
    page, proposal, _, persisted, *_ = fixture
    capture = TransitionCapture(
        "a" * 64, provenance="synthetic_contract", policy_version=VERSION, provider_model=MODEL
    )
    observation = {
        "session_id": page._session.id,
        "frame_id": "frame-1",
        "proposed": proposal["target"],
        "allowed": [
            {"tool": "no_action", "slide_id": page._current_local_state().active_slide_id},
            proposal["target"],
        ],
        "reason": "explicit",
        "elapsed_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    capture.arm(proposal["decision_id"], observation, capture_ns=time.monotonic_ns(), frame_at_ms=0)
    page.transition_observed.connect(capture.completed)
    assert not Trace.model_validate(capture.document()).observations[0].applied
    page._agent_proposed(proposal)
    qtbot.waitUntil(lambda: bool(capture.document()["observations"][0]["applied"]))
    qtbot.waitUntil(lambda: not page.session_runner.busy and bool(persisted))
    trace = Trace.model_validate(capture.document())
    assert trace.observations[0].applied[0].target == trace.observations[0].proposed
    assert trace.observations[0].applied[0].animated is (None if kind == "pptx" else False)
    assert persisted[-1].active_slide_id == proposal["target"]["slide_id"]
    source, context = page._session, fixture[2][0]
    summary = {"status": "available", "text": "Navigation", "reason": None}
    initial = observation["allowed"][0]["slide_id"]
    analysis = {
        "source_format": kind,
        "id": source.analysis_version_id,
        "deck_id": source.deck_id,
        "version": 1,
        "publication": "published",
        "analysis_sha256": None,
        "inputs": None,
        "slides": [
            {"slide_id": identifier, "summary": summary, "actions": []}
            for identifier in (initial, proposal["target"]["slide_id"])
        ],
    }
    frame = {
        "id": "frame-1",
        "at_ms": 0,
        "current": initial,
        "request": {
            "expected_state_version": context.state_version,
            "context_epoch": context.epoch,
            "stream_id": context.stream_id,
            "sequence": 1,
            "sent_at": datetime.now(UTC).isoformat(),
            "text": context.text,
            "latest_utterance": context.text,
            "segments": [{"sequence": 1, "text": context.text}],
        },
        "provider": None,
        "expected": proposal["target"],
        "tags": [],
    }
    corpus = Corpus.model_validate(
        {
            "id": "capture-test",
            "split": "development",
            "permission": {"kind": "synthetic", "reference": "test"},
            "sessions": [
                {"id": source.id, "duration_ms": 10000, "analysis": analysis, "frames": [frame]}
            ],
        }
    )
    gates = Gates.model_validate_json(
        (
            Path(__file__).resolve().parents[1] / "fixtures/replay/development-gates.json"
        ).read_bytes()
    )
    gates = gates.model_copy(update={"corpus_sha256": "a" * 64})
    report = score(corpus, "a" * 64, trace, gates, release=False)
    assert report["metrics"]["target_accuracy"] == 1
    assert report["metrics"]["outside_candidate_set"] == 0
    assert report["metrics"]["unnecessary_animations"] == 0
    assert ("animation_measurement_required" in report["failures"]) == (kind == "pptx")
    assert (
        trace.provenance == "synthetic_contract"
    )  # Mock Office/microphone cannot attest live acceptance.


def test_capture_refuses_unbound_duplicate_or_inferred_completion():
    capture = TransitionCapture(
        "a" * 64, provenance="synthetic_contract", policy_version=VERSION, provider_model=MODEL
    )
    observation = {"session_id": "s", "frame_id": "f"}
    capture.arm("d", observation, capture_ns=100, frame_at_ms=0)
    event = {
        "decision_id": "d",
        "completed_ns": 101,
        "target": {"tool": "no_action", "slide_id": "a"},
        "animated": False,
    }
    with pytest.raises(ValueError):
        capture.completed({**event, "decision_id": "foreign"})
    capture.completed(event)
    with pytest.raises(ValueError):
        capture.completed(event)
    with pytest.raises(ValueError):
        capture.arm(
            "d2",
            {"session_id": "s", "frame_id": "g", "applied": [event]},
            capture_ns=100,
            frame_at_ms=0,
        )
