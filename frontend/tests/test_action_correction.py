"""Corrections cannot change identity or bypass native execution/visual gates."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from threading import Event

import pytest

from buddy_desktop.action_correction import corrected_action, source_target
from buddy_desktop.correction_qa import inspect_with_corrections
from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.preflight import PreflightRunner
from buddy_desktop.slide_contract import ContractError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def correction_source():
    source = (ROOT / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf").read_bytes()
    manifest, _ = open_pdf(source, deck_id="correction-deck", revision="original")
    action = dict(
        version="1.1",
        instance_id="orange",
        purpose="Highlight the whole orange rectangle",
        deck_id=manifest.deck_id,
        render_id=manifest.render_id,
        slide_id=manifest.slides[0].slide_id,
        type="highlight_region",
        params={"region": dict(x=96, y=410, width=240, height=240)},
    )
    return source, manifest, action


def test_source_measured_bounds_and_identity_preserved(correction_source):
    source, manifest, action = correction_source
    original = deepcopy(action)
    bounds, dimensions = source_target(source, action, manifest, {})
    assert bounds == pytest.approx(dict(x=96, y=410, width=480, height=240))
    corrected = corrected_action(
        action,
        dict(x=-100, y=-100, width=10000, height=10000),
        bounds,
        dimensions,
        manifest,
    )
    assert action == original
    assert {k: v for k, v in corrected.items() if k != "params"} == {
        k: v for k, v in action.items() if k != "params"
    }
    region = corrected["params"]["region"]
    assert region["x"] >= 0 and region["y"] >= 0
    assert region["width"] <= bounds["width"] * 1.3 + 1
    assert region["height"] <= bounds["height"] * 1.3 + 1


@pytest.mark.parametrize(
    "proposal",
    [
        {"x": 0, "y": 0, "width": 30, "height": 30},
        {"x": 96, "y": 410, "width": float("nan"), "height": 240},
        {"x": 96, "y": 410, "width": True, "height": 240},
        {"x": 96, "y": 410, "width": 480, "height": 240, "slide_id": "other"},
        {"x": 96, "y": 410, "width": 0, "height": 240},
    ],
)
def test_invalid_corrections_fail_before_executor(correction_source, proposal):
    source, manifest, action = correction_source
    bounds, dimensions = source_target(source, action, manifest, {})
    with pytest.raises(ContractError):
        corrected_action(action, proposal, bounds, dimensions, manifest)


def test_real_pdf_correction_replays_and_persists_every_version(
    correction_source,
    tmp_path,
    qapp,
):
    source, manifest, action = correction_source
    calls = []
    bounds, _ = source_target(source, action, manifest, {})

    def judge(target, before, after):
        calls.append(target)
        assert before != after
        assert target["purpose"] == action["purpose"]
        if len(calls) == 1:
            return dict(
                verdict="rejected",
                confidence=0.95,
                issue="partial_target",
                observation="Only the left half of the orange box is highlighted.",
                correction=bounds,
            )
        return dict(
            verdict="validated",
            confidence=0.92,
            issue=None,
            observation="The orange box is completely highlighted.",
            correction=None,
        )

    profile = "wide-2x" if qapp.primaryScreen().devicePixelRatio() == 2 else "wide-1x"
    with PreflightRunner("test-owner") as runner:
        report = inspect_with_corrections(
            runner,
            "test-owner",
            source=source,
            source_format="pdf",
            deck_id=manifest.deck_id,
            revision="original",
            slides={s.slide_id: {} for s in manifest.slides},
            action=action,
            model_id="controlled-provider",
            judge=judge,
            output=tmp_path / "history",
            profile=profile,
        )
    if report["state"] == "unavailable":
        assert not report["semantic_verified"]
        pytest.skip("Native capture unavailable on this host: " + str(report["reason"]))
    assert report["state"] == "validated", report
    assert len(calls) == 2
    assert len(report["history"]) == 2
    assert report["history"][0]["state"] == "rejected"
    assert report["history"][1]["state"] == "validated"
    assert report["original_action_sha256"] != report["action_sha256"]
    assert report["corrected_action"]["params"]["region"] == bounds
    assert hashlib.sha256(source).hexdigest() == report["preflight"]["source_sha256"]
    assert json.loads((tmp_path / "history/report.json").read_bytes()) == report
    for index in (0, 1):
        folder = tmp_path / "history" / f"attempt-{index}"
        assert (folder / "report.json").exists()
        assert (folder / "before.png").exists() and (folder / "after.png").exists()


def test_native_shape_bounds_are_independent_of_model_coordinates():
    source = (ROOT / "frontend/tests/fixtures/slide_actions/wide.pptx").read_bytes()
    region = dict(x=40.0, y=32.0, width=880.0, height=46.04527559055118)
    action = dict(
        source_format="pptx",
        version="powerpoint-2",
        instance_id="title",
        purpose="Emphasize the title",
        deck_id="test",
        render_id="pptx-" + hashlib.sha256(source).hexdigest(),
        slide_id="slide-1",
        type="highlight_region",
        params=dict(region=region, source_shape_id=2),
    )
    slides = {"slide-1": dict(number=1, native_id=256, width=960, height=540)}
    target, size = source_target(source, action, None, slides)
    assert target == pytest.approx(region)
    with pytest.raises(ContractError, match="measured by PowerPoint"):
        corrected_action(action, {**region, "width": region["width"] + 3}, target, size, None)
    # A coordinate-bound annotation can be corrected, keeping its exact identity.
    unbound = deepcopy(action)
    del unbound["params"]["source_shape_id"]
    unbound["params"]["region"]["width"] = 440
    candidate = corrected_action(unbound, region, target, size, None)
    assert candidate["params"]["region"] == pytest.approx(region)
    assert candidate["instance_id"] == unbound["instance_id"]


def test_owner_and_attempt_limits(correction_source, tmp_path):
    source, manifest, action = correction_source
    with PreflightRunner("owner") as runner:
        for owner, count, error in (("foreign", 1, PermissionError), ("owner", 3, ContractError)):
            with pytest.raises(error):
                inspect_with_corrections(
                    runner,
                    owner,
                    source=source,
                    source_format="pdf",
                    deck_id=manifest.deck_id,
                    revision="original",
                    slides={s.slide_id: {} for s in manifest.slides},
                    action=action,
                    model_id="test",
                    judge=lambda *_: pytest.fail("must not call provider"),
                    output=tmp_path / "denied",
                    max_corrections=count,
                )
        cancelled = Event()
        cancelled.set()
        report = inspect_with_corrections(
            runner,
            "owner",
            source=source,
            source_format="pdf",
            deck_id=manifest.deck_id,
            revision="original",
            slides={s.slide_id: {} for s in manifest.slides},
            action=action,
            model_id="test",
            judge=lambda *_: pytest.fail("must not call provider"),
            output=tmp_path / "cancelled",
            cancelled=cancelled,
        )
    assert report["state"] == "unavailable" and report["reason"] == "cancelled"
    assert len(report["history"]) == 1 and report["corrected_action"] is None


def test_retry_exhaustion_keeps_corrected_action_ineligible(correction_source, tmp_path, qapp):
    source, manifest, action = correction_source
    bounds, _ = source_target(source, action, manifest, {})
    calls = []

    def judge(*_):
        calls.append(True)
        return dict(
            verdict="rejected",
            confidence=0.9,
            issue="clipped",
            observation="The intended target is still clipped.",
            correction=bounds,
        )

    profile = "wide-2x" if qapp.primaryScreen().devicePixelRatio() == 2 else "wide-1x"
    with PreflightRunner("owner") as runner:
        report = inspect_with_corrections(
            runner,
            "owner",
            source=source,
            source_format="pdf",
            deck_id=manifest.deck_id,
            revision="original",
            slides={s.slide_id: {} for s in manifest.slides},
            action=action,
            model_id="controlled-provider",
            judge=judge,
            output=tmp_path / "exhausted",
            profile=profile,
            max_corrections=1,
        )
    if report["state"] == "unavailable":
        assert not report["semantic_verified"]
        pytest.skip("Native capture unavailable: " + str(report["reason"]))
    assert len(calls) == 2 and len(report["history"]) == 2
    assert report["stop_reason"] == "attempt_limit"
    assert report["state"] == "rejected"
    assert report["corrected_action"] is None
    assert not report["semantic_verified"]
