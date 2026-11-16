"""Fresh, eligible speech proposals can run without a local calibration profile."""

import json
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot
from buddy_desktop.transition_calibration import build
from buddy_desktop.transition_policy import (
    MODEL,
    VERSION,
    Prediction,
    Settings,
    Target,
    TransitionPolicy,
    View,
    load_calibration,
)

TODAY = date(2026, 11, 15)
NOW = 10_000_000_000
NEXT = Target("next_slide", "two")
PREVIOUS = Target("previous_slide", "two")
NUMBERED = Target("go_to_slide", "three")
ZOOM = Target("zoom_region", "one", "a", (10, 10, 100, 100))


def inputs(sequence=1, now=NOW, *, source_format="pdf", target=NEXT, source="model"):
    context = ContextSnapshot(
        "session",
        3,
        4,
        "stream",
        "en-US",
        (
            ContextSegment(
                str(sequence),
                sequence,
                "Explain the sales comparison.",
                True,
                sequence,
                sequence + 1,
                now,
            ),
        ),
        now,
        True,
    )
    prediction = Prediction(
        str(uuid4()),
        target,
        source,
        "explicit" if source == "navigation" else "topic",
        0.99,
        (sequence,),
        "session",
        "analysis",
        3,
        4,
        "stream",
        sequence,
    )
    view = View(
        "session",
        "analysis",
        "sourcehash",
        source_format,
        3,
        Target("slide", "one"),
        (NEXT, PREVIOUS, NUMBERED, ZOOM),
    )
    return prediction, context, view


def enabled():
    policy = TransitionPolicy()
    policy.set_enabled(True)
    return policy


def consider(policy, bundle, now=NOW):
    return policy.consider(*bundle, now_ns=now, today=TODAY)


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_one_fresh_model_proposal_is_accepted_without_calibration(source_format):
    policy = enabled()
    proposal = inputs(source_format=source_format)
    assert policy.calibration_sha256 is None
    assert consider(policy, proposal) == "accepted"
    assert policy.enabled


@pytest.mark.parametrize(
    "target",
    [
        Target("next_slide", "two"),
        Target("previous_slide", "two"),
        Target("go_to_slide", "three"),
    ],
)
def test_explicit_navigation_including_numbered_slide_bypasses_model(target):
    policy = enabled()
    proposal = inputs(source="navigation", target=target)
    assert consider(policy, proposal) == "accepted"


def test_failed_execution_keeps_opt_in_and_requires_fresh_speech():
    policy = enabled()
    proposal = inputs(source="navigation")
    assert consider(policy, proposal) == "accepted"
    policy.completed(proposal[0].decision_id, success=False, now_ns=NOW)
    assert policy.enabled
    assert consider(policy, proposal) == "duplicate"
    assert consider(policy, inputs(2, NOW + 500_000_000), NOW + 500_000_000) == "cooldown"
    assert consider(policy, inputs(3, NOW + 900_000_000), NOW + 900_000_000) == "accepted"


def test_duplicate_http_decisions_for_same_completed_speech_cannot_repeat():
    policy = enabled()
    proposal = inputs()
    assert consider(policy, proposal) == "accepted"
    policy.completed(proposal[0].decision_id, success=True, now_ns=NOW)
    repeated = replace(proposal[0], decision_id=str(uuid4()))
    assert consider(policy, (repeated, *proposal[1:]), NOW + 800_000_000) == "duplicate"


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "old",
        "future",
        "session",
        "analysis",
        "version",
        "epoch",
        "stream",
        "connection",
        "busy",
        "ineligible",
        "missing_evidence",
        "bad_confidence",
        "unknown_source",
        "nonexplicit_navigation",
    ],
)
def test_invalid_or_stale_decisions_are_rejected(change):
    policy = enabled()
    prediction, context, view = inputs()
    now = NOW
    if change == "partial":
        context = replace(
            context, segments=(*context.segments, replace(context.segments[-1], final=False))
        )
    elif change == "old":
        now += 6_000_000_000
    elif change == "future":
        now -= 1
    elif change in {"session", "analysis", "version"}:
        field = {"session": "session_id", "analysis": "analysis_id", "version": "state_version"}[
            change
        ]
        view = replace(view, **{field: 99 if change == "version" else "other"})
    elif change in {"epoch", "stream"}:
        context = replace(
            context,
            **{change if change == "epoch" else "stream_id": 5 if change == "epoch" else "other"},
        )
    elif change == "connection":
        view = replace(view, connected=False)
    elif change == "busy":
        view = replace(view, busy=True)
    elif change == "ineligible":
        view = replace(view, eligible=())
    elif change == "missing_evidence":
        prediction = replace(prediction, evidence_sequences=())
    elif change == "bad_confidence":
        prediction = replace(prediction, confidence=float("nan"))
    elif change == "unknown_source":
        prediction = replace(prediction, source="fallback")
    elif change == "nonexplicit_navigation":
        prediction = replace(prediction, source="navigation", reason="topic")
    result = consider(policy, (prediction, context, view), now)
    assert result in {"stale", "busy", "ineligible", "weak_evidence"}
    assert policy.enabled


def test_current_focus_no_action_and_manual_hold_abstain():
    policy = enabled()
    proposal = inputs(target=ZOOM)
    assert consider(policy, (proposal[0], proposal[1], replace(proposal[2], current=ZOOM))) == (
        "current_focus"
    )
    no_action = inputs(target=Target("no_action", "one"))
    assert consider(policy, no_action) == "no_action"
    policy.reset(manual=True, now_ns=NOW)
    assert consider(policy, inputs(2, NOW + 1_000_000_000), NOW + 1_000_000_000) == "cooldown"
    assert consider(policy, inputs(3, NOW + 6_000_000_000), NOW + 6_000_000_000) == "accepted"


def test_calibration_builder_remains_available_for_offline_evaluation(tmp_path):
    path, output = tmp_path / "development.json", tmp_path / "profile.json"
    raw = {
        "protocol": "transition-calibration-input.v1",
        "split": "development",
        "model": MODEL,
        "source_format": "pdf",
        "permission": {"kind": "synthetic", "reference": "unit test", "expires": "2099-01-01"},
        "samples": [{"id": str(i), "confidence": 0.95, "correct": True} for i in range(200)],
    }
    path.write_text(json.dumps(raw))
    build(path, 0.9, output)
    profile = load_calibration(output)
    assert profile.samples == 200 and profile.lower_precision > 0.95
    assert not profile.usable("pdf", TODAY)  # Synthetic evidence never enables live autonomy.
    assert json.loads(output.read_text())["protocol"] == VERSION
    with pytest.raises(ValueError):
        build(path, 0.9, output)
    raw["split"] = "holdout"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        build(path, 0.9, tmp_path / "other.json")


@pytest.mark.parametrize("values", [(-1, 0), (0, -1), (60_001, 0)])
def test_invalid_policy_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Settings(*values)
