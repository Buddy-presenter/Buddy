"""Real policy rules with captured clocks, frozen calibration and no provider calls."""

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
    Calibration,
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
        "session", "analysis", "sourcehash", source_format, 3, Target("slide", "one"), (NEXT, ZOOM)
    )
    return prediction, context, view


def calibrated(source_format="pdf"):
    policy = TransitionPolicy()
    policy.calibration = Calibration(source_format, 0.9, 200, 200, "a" * 64, TODAY, True)
    policy.set_enabled(True)
    return policy


def consider(policy, bundle, now=NOW):
    return policy.consider(*bundle, now_ns=now, today=TODAY)


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_two_distinct_completed_references_persist_then_complete(source_format):
    policy = calibrated(source_format)
    first = inputs(source_format=source_format)
    assert consider(policy, first) == "awaiting_evidence"
    # Another HTTP decision about the same evidence is not a confirmation.
    assert (
        consider(policy, (replace(first[0], decision_id=str(uuid4())), *first[1:]))
        == "awaiting_evidence"
    )
    second = inputs(2, NOW + 900_000_000, source_format=source_format)
    assert consider(policy, second, NOW + 900_000_000) == "accepted"
    assert consider(policy, inputs(3, NOW + 1_000_000_000), NOW + 1_000_000_000) == "busy"
    policy.completed(second[0].decision_id, success=True, now_ns=NOW + 1_000_000_000)
    assert consider(policy, inputs(3, NOW + 1_100_000_000), NOW + 1_100_000_000) == "cooldown"


def test_uncalibrated_and_low_quality_calibration_never_move_model_targets():
    policy = TransitionPolicy()
    policy.set_enabled(True)
    assert consider(policy, inputs()) == "uncalibrated"
    good = calibrated().calibration
    for profile in (
        replace(good, samples=99, successes=99),
        replace(good, successes=160),
        replace(good, consented=False),
        replace(good, expires=date(2026, 11, 14)),
        replace(good, source_format="pptx"),
    ):
        policy.calibration = profile
        assert consider(policy, inputs()) == "uncalibrated"


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_request_watermark_can_differ_from_latest_utterance_without_false_confirmation(
    source_format,
):
    policy = calibrated(source_format)
    prediction, context, view = inputs(source_format=source_format)
    latest = context.segments[-1]
    earlier = replace(
        latest,
        segment_id="earlier",
        sequence=2,
        start_seconds=0,
        end_seconds=1,
        capture_end_ns=NOW - 1_000_000_000,
    )
    context = replace(context, segments=(earlier, latest))
    prediction = replace(prediction, sequence=2)
    assert consider(policy, (prediction, context, view)) == "awaiting_evidence"

    # A newer revision/watermark elsewhere in the window is not new latest evidence.
    revised = replace(context, segments=(replace(earlier, sequence=3), latest))
    repeated = replace(prediction, decision_id=str(uuid4()), sequence=3)
    assert consider(policy, (repeated, revised, view), NOW + 900_000_000) == "awaiting_evidence"

    new = inputs(4, NOW + 1_800_000_000, source_format=source_format)
    assert consider(policy, new, NOW + 1_800_000_000) == "accepted"


def test_wrong_request_watermark_still_rejects_and_clears_pending_evidence():
    policy = calibrated()
    assert consider(policy, inputs()) == "awaiting_evidence"
    prediction, context, view = inputs(2, NOW + 900_000_000)
    assert (
        consider(policy, (replace(prediction, sequence=3), context, view), NOW + 900_000_000)
        == "stale"
    )
    assert (
        consider(policy, inputs(3, NOW + 1_800_000_000), NOW + 1_800_000_000) == "awaiting_evidence"
    )


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
    ],
)
def test_stale_and_boundary_changes_clear_sustained_evidence(change):
    policy = calibrated()
    assert consider(policy, inputs()) == "awaiting_evidence"
    p, c, v = inputs(2, NOW + 1_000_000_000)
    now = NOW + 1_000_000_000
    if change == "partial":
        c = replace(c, segments=(*c.segments, replace(c.segments[-1], final=False)))
    elif change == "old":
        now += 6_000_000_000
    elif change == "future":
        now -= 1
    elif change in {"session", "analysis", "version"}:
        field = {"session": "session_id", "analysis": "analysis_id", "version": "state_version"}[
            change
        ]
        v = replace(v, **{field: 99 if change == "version" else "other"})
    elif change in {"epoch", "stream"}:
        c = replace(
            c, **{change if change == "epoch" else "stream_id": 5 if change == "epoch" else "other"}
        )
    elif change == "connection":
        v = replace(v, connected=False)
    elif change == "busy":
        v = replace(v, busy=True)
    elif change == "ineligible":
        v = replace(v, eligible=())
    assert consider(policy, (p, c, v), now) != "accepted"
    assert (
        consider(policy, inputs(3, NOW + 2_000_000_000), NOW + 2_000_000_000) == "awaiting_evidence"
    )


def test_conflicts_no_action_silence_and_manual_changes_restart_persistence():
    policy = calibrated()
    assert consider(policy, inputs()) == "awaiting_evidence"
    assert (
        consider(policy, inputs(2, NOW + 1_000_000_000, target=ZOOM), NOW + 1_000_000_000)
        == "awaiting_evidence"
    )
    assert (
        consider(policy, inputs(3, NOW + 2_000_000_000), NOW + 2_000_000_000) == "awaiting_evidence"
    )
    assert (
        consider(
            policy,
            inputs(4, NOW + 3_000_000_000, target=Target("no_action", "one")),
            NOW + 3_000_000_000,
        )
        == "no_action"
    )
    assert (
        consider(policy, inputs(5, NOW + 4_000_000_000), NOW + 4_000_000_000) == "awaiting_evidence"
    )
    policy.reset(manual=True, now_ns=NOW + 4_000_000_000)
    assert consider(policy, inputs(6, NOW + 5_000_000_000), NOW + 5_000_000_000) == "cooldown"
    assert (
        consider(policy, inputs(7, NOW + 10_000_000_000), NOW + 10_000_000_000)
        == "awaiting_evidence"
    )


def test_current_focus_never_retriggers_and_switch_needs_stronger_longer_evidence():
    policy = calibrated()
    p, c, v = inputs(target=ZOOM)
    assert consider(policy, (p, c, replace(v, current=ZOOM))) == "current_focus"
    view = replace(v, current=ZOOM)
    for sequence in range(1, 4):
        now = NOW + sequence * 1_000_000_000
        p, c, _ = inputs(sequence, now)
        assert consider(policy, (replace(p, confidence=0.94), c, view), now) == "weak_evidence"
    for sequence in range(4, 7):
        now = NOW + sequence * 1_000_000_000
        p, c, _ = inputs(sequence, now)
        expected = "accepted" if sequence == 6 else "awaiting_evidence"
        assert consider(policy, (p, c, view), now) == expected


def test_navigation_uses_production_fast_path_without_model_calibration_and_failure_disables():
    policy = TransitionPolicy()
    policy.set_enabled(True)
    values = inputs(source="navigation")
    assert consider(policy, values) == "accepted"
    policy.completed(values[0].decision_id, success=False, now_ns=NOW)
    assert not policy.enabled
    assert consider(policy, inputs()) == "disabled"
    policy.set_enabled(True)
    assert consider(policy, values, NOW + 6_000_000_000) == "stale"


@pytest.mark.parametrize("change", ["confidence", "short", "duration", "evidence", "fallback"])
def test_weak_or_unfinished_evidence_never_accumulates(change):
    policy = calibrated()
    p, c, v = inputs()
    if change == "confidence":
        p = replace(p, confidence=float("nan"))
    elif change == "evidence":
        p = replace(p, evidence_sequences=())
    elif change == "fallback":
        p = replace(p, source="fallback")
    elif change == "short":
        c = replace(c, segments=(replace(c.segments[0], text="Sales"),))
    else:
        c = replace(c, segments=(replace(c.segments[0], end_seconds=1.1),))
    assert consider(policy, (p, c, v)) == "weak_evidence"


def test_calibration_builder_freezes_development_only_and_never_overwrites(tmp_path):
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


def test_invalid_policy_settings_are_rejected():
    with pytest.raises(ValueError):
        Settings(confirmations=1)
    with pytest.raises(ValueError):
        Settings(persistence_ms=0)
