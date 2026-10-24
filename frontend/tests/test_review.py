import hashlib
import json
import time
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QPushButton

import buddy_desktop.review_widget as review_widget
from buddy_desktop.auth import Tokens
from buddy_desktop.config import AppConfig
from buddy_desktop.presentation_service import DownloadedPDF
from buddy_desktop.review_service import ReviewService, parse_analysis
from buddy_desktop.services import ServiceError


def analysis_payload(deck_id=None, source_sha256="b" * 64):
    deck_id = deck_id or uuid4()
    analysis_id = uuid4()
    digest = "a" * 64
    slide_id = "slide-1"
    action = {
        "version": "1.1",
        "instance_id": "action-1",
        "purpose": "Focus on the chart",
        "deck_id": str(deck_id),
        "render_id": "render-1",
        "slide_id": slide_id,
        "type": "highlight_region",
        "params": {"region": {"x": 10, "y": 20, "width": 30, "height": 40}},
    }
    return deck_id, {
        "data": {
            "id": str(analysis_id),
            "deck_id": str(deck_id),
            "version": 2,
            "publication": "awaiting_validation",
            "analysis_sha256": digest,
            "inputs": {
                "source_revision": "revision-1",
                "source_sha256": source_sha256,
                "display_profile": "wide-1x",
                "validator_version": "validator-1",
                "renderer_version": "renderer-1",
                "presentation_summary": {"status": "available", "text": "Overall summary"},
                "manifest": {
                    "deck_id": str(deck_id),
                    "render_id": "render-1",
                    "slides": [
                        {
                            "slide_id": slide_id,
                            "source": {"width": 100, "height": 100},
                            "raster": {"width": 100, "height": 100},
                        }
                    ],
                },
            },
            "slides": [
                {
                    "slide_id": slide_id,
                    "summary": {"status": "available", "text": "Slide summary"},
                    "actions": [
                        {
                            "action": action,
                            "state": "candidate",
                            "usable": False,
                            "validation": None,
                        }
                    ],
                }
            ],
        },
        "request_id": "test",
    }


def test_review_parser_reads_versioned_summary_and_actions():
    deck_id, payload = analysis_payload()
    result = parse_analysis(payload, deck_id)
    assert result.version == 2
    assert result.publication == "awaiting_validation"
    assert result.action_limit_reached is False
    assert result.presentation_summary.text == "Overall summary"
    assert result.slides[0].summary.text == "Slide summary"
    action = result.slides[0].actions[0]
    assert action.region == (10, 20, 30, 40)
    assert action.state == "candidate" and not action.usable


def test_review_parser_reports_action_capacity_without_changing_analysis_hash_inputs():
    deck_id, payload = analysis_payload()
    payload["data"]["action_limit_reached"] = True
    result = parse_analysis(payload, deck_id)
    assert result.action_limit_reached

    payload["data"]["action_limit_reached"] = "true"
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_analysis(payload, deck_id)


@pytest.mark.parametrize("mutation", ["foreign_deck", "outside_region", "invalid_summary"])
def test_review_parser_rejects_inconsistent_records(mutation):
    deck_id, payload = analysis_payload()
    if mutation == "foreign_deck":
        payload["data"]["deck_id"] = str(uuid4())
    elif mutation == "outside_region":
        payload["data"]["slides"][0]["actions"][0]["action"]["params"]["region"]["x"] = 90
    else:
        payload["data"]["inputs"]["presentation_summary"] = {"status": "available", "text": None}
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_analysis(payload, deck_id)


def test_review_parser_binds_validation_evidence_to_action_and_profile():
    deck_id, payload = analysis_payload()
    data = payload["data"]
    action_result = data["slides"][0]["actions"][0]
    action = action_result["action"]
    validation_id, artifact_id = uuid4(), uuid4()
    action_hash = hashlib.sha256(
        json.dumps(action, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    action_result.update(
        state="validated",
        usable=True,
        validation={
            "schema_version": "buddy.validation.v1",
            "validation_id": str(validation_id),
            "analysis_id": data["id"],
            "analysis_sha256": data["analysis_sha256"],
            "instance_id": action["instance_id"],
            "action_sha256": action_hash,
            "validator_version": "validator-1",
            "renderer_version": "renderer-1",
            "display_profile": "wide-1x",
            "state": "validated",
            "reasons": [],
            "evidence": [{"id": str(artifact_id), "kind": "before_image", "sha256": "c" * 64}],
        },
    )
    result = parse_analysis(payload, deck_id)
    item = result.slides[0].actions[0]
    assert item.validation_id == validation_id
    assert item.usable and item.state == "validated"
    assert item.evidence[0].id == artifact_id
    payload["data"]["slides"][0]["actions"][0]["validation"]["action_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        parse_analysis(payload, deck_id)


def test_review_service_uses_owner_scoped_latest_analysis_path(backend, monkeypatch):
    deck_id, payload = analysis_payload()
    backend.body = json.dumps(payload).encode()
    service = ReviewService(
        AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5), "private-token"
    )
    result = service.analysis(deck_id)
    assert result.deck_id == deck_id
    assert backend.entered.is_set()


def test_review_evidence_checksum_is_verified(backend, monkeypatch):
    content = b'{"state":"validated"}'
    backend.body = content
    backend.content_type = "application/json"
    service = ReviewService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5), "token")
    body, mime = service.evidence(uuid4(), uuid4(), hashlib.sha256(content).hexdigest())
    assert body == content and mime == "application/json"
    with pytest.raises(ServiceError, match="integrity"):
        service.evidence(uuid4(), uuid4(), "0" * 64)


def test_review_widget_loads_read_only_saved_page_and_clears_on_signout(
    desktop_window, monkeypatch, qtbot
):
    content = b"%PDF-1.7 saved review fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck: record)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda _self, _deck: DownloadedPDF(content, str(deck_id), "revision-1"),
    )
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    monkeypatch.setattr(
        review_widget,
        "render",
        lambda _content, _index: ({"sizes": [[100, 100]]}, image.copy()),
    )
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck_id, "Review fixture")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.content.isHidden())
    assert not review.content.isHidden()
    assert "Overall summary" in review.summary.text()
    assert "Slide summary" in review.slide_summary.text()
    assert review.action_table.rowCount() == 1
    assert "not usable" in review.action_detail.toPlainText()
    assert not any("execute" in child.text().lower() for child in review.findChildren(QPushButton))
    window.account.session.tokens = None
    window.account.session_changed.emit()
    assert review.pdf_content is None
    assert review.analysis is None
    assert review.action_table.rowCount() == 0


def test_review_queues_new_deck_while_stale_load_is_finishing(desktop_window, monkeypatch, qtbot):
    content = b"%PDF-1.7 queued review fixture"
    digest = hashlib.sha256(content).hexdigest()
    first_deck, first_payload = analysis_payload(source_sha256=digest)
    second_deck, second_payload = analysis_payload(source_sha256=digest)
    first_record = parse_analysis(first_payload, first_deck)
    second_record = parse_analysis(second_payload, second_deck)
    first_started, release_first = Event(), Event()
    second_started, release_second = Event(), Event()
    calls = []

    def analysis(_service, deck_id):
        calls.append(deck_id)
        if deck_id == first_deck:
            first_started.set()
            if not release_first.wait(5):
                raise TimeoutError("First review request was not released.")
            return first_record
        second_started.set()
        if not release_second.wait(5):
            raise TimeoutError("Second review request was not released.")
        return second_record

    monkeypatch.setattr(ReviewService, "analysis", analysis)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda _self, deck_id: DownloadedPDF(content, str(deck_id), "revision-1"),
    )
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    monkeypatch.setattr(
        review_widget,
        "render",
        lambda _content, _index: ({"sizes": [[100, 100]]}, image.copy()),
    )
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review

    try:
        review.set_deck(first_deck, "First deck")
        review.activate(True)
        qtbot.waitUntil(first_started.is_set)

        review.activate(False)
        review.set_deck(second_deck, "Second deck")
        review.activate(True)
        assert review.pending_load

        release_first.set()
        qtbot.waitUntil(second_started.is_set)
        assert calls == [first_deck, second_deck]
        assert review.analysis is None

        release_second.set()
        qtbot.waitUntil(lambda: not review.runner.busy and not review.content.isHidden())
        assert review.heading.text() == "Second deck"
        assert review.analysis is second_record
    finally:
        release_first.set()
        release_second.set()
