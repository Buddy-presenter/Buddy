import hashlib
import json
import time
from copy import deepcopy
from dataclasses import replace
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


class FakeSession:
    """Stand-in signed-in session: same token fields and fresh_access as AccountSession."""

    def __init__(self, tokens, cancelled):
        self.tokens, self.cancelled = tokens, cancelled

    def fresh_access(self, margin=60):
        return self.tokens.access


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
        semantic_verified=True,
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
            "validation_kind": "visual",
            "semantic_verified": True,
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
    legacy = deepcopy(payload)
    legacy_result = legacy["data"]["slides"][0]["actions"][0]
    del legacy_result["semantic_verified"]
    del legacy_result["validation"]["semantic_verified"]
    del legacy_result["validation"]["validation_kind"]
    legacy_action = parse_analysis(legacy, deck_id).slides[0].actions[0]
    assert legacy_action.state == "validated"
    assert not legacy_action.usable and not legacy_action.semantic_verified
    assert legacy_action.validation_kind == "execution"
    # An explicit new-format execution verdict must not claim usability.
    legacy_result["semantic_verified"] = False
    with pytest.raises(ValueError):
        parse_analysis(legacy, deck_id)
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
    desktop_window, monkeypatch, qtbot, tmp_path
):
    content = b"%PDF-1.7 saved review fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck, _format="pdf": record)
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
    window.account.session = FakeSession(
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
    window.account.account_id = "test-owner"
    submissions = []
    tokens_used = []
    second_started, release_second = Event(), Event()

    def qa(_self, analysis_id, instance_id, **options):
        submissions.append((analysis_id, instance_id))
        tokens_used.append(_self.access)
        assert options["correct"] is True
        if instance_id == "second-action":
            second_started.set()
            if not release_second.wait(5):
                raise TimeoutError("Second QA action was not released.")
            return dict(state="rejected", reasons=["wrong_target"])
        return dict(state="validated", reasons=[])

    monkeypatch.setattr(review_widget.PreflightService, "run", qa)
    # Pending actions are checked automatically: progress shows and no manual
    # run-all, refresh or correction controls are offered.
    assert "Checking actions · 0 of 1 checked" in review.check_label.text()
    assert not review.check_progress.isHidden() and review.check_poll.isActive()
    assert review.visual_qa_button.isHidden()
    assert review.visual_qa_all_button.isHidden() and review.refresh_checks_button.isHidden()
    assert review.visual_qa_correct.isHidden()
    # A finished, failed check offers Check again, which always tries corrections.
    rejected = replace(
        record.slides[0].actions[0],
        state="rejected",
        reasons=("wrong_target",),
        validation_kind="visual",
    )
    review.analysis = replace(record, slides=(replace(record.slides[0], actions=(rejected,)),))
    review._display_page()
    assert "All actions checked · 0 of 1 Ready" in review.check_label.text()
    assert not review.check_poll.isActive()
    assert not review.visual_qa_button.isHidden() and review.visual_qa_button.isEnabled()
    assert review.visual_qa_button.text() == "Check again"
    review.visual_qa_button.click()
    qtbot.waitUntil(lambda: "Checked 1 of 1: 1 Ready" in review.visual_qa_status.text())
    assert submissions == [(record.id, record.slides[0].actions[0].instance_id)]
    # Once anything is checked, Check all again reruns every action, Ready ones too.
    validated = replace(rejected, state="validated", usable=True, semantic_verified=True)
    review.analysis = replace(record, slides=(replace(record.slides[0], actions=(validated,)),))
    review._display_page()
    qtbot.waitUntil(review.visual_qa_all_button.isEnabled)
    assert not review.visual_qa_all_button.isHidden()
    assert review.visual_qa_all_button.text() == "Check all again"
    submissions.clear()
    tokens_used.clear()
    # Access tokens rotate during long runs: each check uses the session's current one.
    window.account.session.fresh_access = lambda margin=60: "rotated-access-token"
    review.visual_qa_all_button.click()
    assert review.visual_qa_all_button.isHidden()  # hidden while the run is in progress
    qtbot.waitUntil(lambda: "Checked 1 of 1: 1 Ready" in review.visual_qa_status.text())
    assert tokens_used == ["rotated-access-token"]
    assert submissions == [(record.id, validated.instance_id)]
    window.account.session.tokens = None
    window.account.session_changed.emit()
    assert review.visual_qa_status.text() == ""
    assert review.check_progress.isHidden() and not review.check_poll.isActive()
    assert review.pdf_content is None
    assert review.analysis is None
    assert review.action_table.rowCount() == 0


def test_check_all_again_is_clickable_after_opening_a_checked_deck(
    desktop_window, monkeypatch, qtbot
):
    content = b"%PDF-1.7 checked review fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    checked = replace(
        record.slides[0].actions[0],
        state="validated",
        usable=True,
        semantic_verified=True,
        validation_kind="visual",
    )
    record = replace(record, slides=(replace(record.slides[0], actions=(checked,)),))
    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck, _format="pdf": record)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda _self, _deck: DownloadedPDF(content, str(deck_id), "revision-1"),
    )
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    monkeypatch.setattr(
        review_widget, "render", lambda _content, _index: ({"sizes": [[100, 100]]}, image.copy())
    )
    window = desktop_window()
    window.account.session = FakeSession(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck_id, "Checked fixture")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.content.isHidden())
    # The button first appears while Review is still loading; once idle it must be usable.
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert not review.visual_qa_all_button.isHidden()
    assert review.visual_qa_all_button.isEnabled()


def test_near_expiry_account_check_keeps_review_visible(desktop_window, monkeypatch, qtbot):
    content = b"%PDF-1.7 account refresh review fixture"
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
    window.account.session = FakeSession(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.account_id = "same-account"
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck_id, "Review fixture")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)

    window.account.session.tokens = Tokens("near-expiry", "refresh", time.monotonic() + 10)
    window.account.operation = "check"
    window.account.activity_changed.emit(True)
    assert review.analysis is record
    assert review.pdf_content == content
    assert not review.content.isHidden()

    window.account.activity_changed.emit(False)
    window.account.session_changed.emit()
    assert review.analysis is record
    assert review.pdf_content == content
    assert not review.content.isHidden()
    assert review.signin.isHidden()


def test_review_exactly_40_actions_reports_capacity_without_claiming_truncation(
    desktop_window, monkeypatch, qtbot
):
    content = b"%PDF-1.7 exact action capacity fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    data = payload["data"]
    template = data["slides"][0]["actions"][0]
    actions = []
    for index in range(40):
        action = deepcopy(template)
        action["action"]["instance_id"] = f"action-{index + 1}"
        actions.append(action)
    data["slides"][0]["actions"] = actions
    data["action_limit_reached"] = True
    record = parse_analysis(payload, deck_id)
    assert sum(len(slide.actions) for slide in record.slides) == 40

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
    window.account.session = FakeSession(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck_id, "Capacity fixture")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.content.isHidden())

    assert review.notice.text() == (
        "The PDF action inventory reached a processing limit; some targets may be missing. "
        "Review every slide before presenting."
    )


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
    window.account.session = FakeSession(
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


def _signed_in_review(desktop_window, account_id="same-account"):
    window = desktop_window()
    window.account.session = FakeSession(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.account_id = account_id
    window.account.session_changed.emit()
    return window


def test_review_downloads_source_during_analysis_and_reuses_it_on_reopen(
    desktop_window, monkeypatch, qtbot
):
    content = b"%PDF-1.7 cached review fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    source_started = Event()
    downloads = []

    def analysis(_self, _deck):
        # The first open must not wait for analysis before fetching the PDF.
        if not downloads and not source_started.wait(5):
            raise TimeoutError("Source download did not start alongside the analysis.")
        return record

    def source(_self, _deck):
        downloads.append(_deck)
        source_started.set()
        return DownloadedPDF(content, str(deck_id), "revision-1")

    monkeypatch.setattr(ReviewService, "analysis", analysis)
    monkeypatch.setattr(ReviewService, "source", source)
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    monkeypatch.setattr(
        review_widget, "render", lambda _content, _index: ({"sizes": [[100, 100]]}, image.copy())
    )
    window = _signed_in_review(desktop_window)
    review = window.review
    review.set_deck(deck_id, "Cached deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)
    assert review.analysis is record and downloads == [deck_id]

    review.activate(False)
    review.set_deck(deck_id, "Cached deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)
    assert review.analysis is record and review.pdf_content == content
    assert downloads == [deck_id]

    # A changed source revision is downloaded again rather than trusted from memory.
    changed = replace(record, source_revision="revision-2")
    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck: changed)
    review.activate(False)
    review.set_deck(deck_id, "Cached deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)
    assert downloads == [deck_id, deck_id]


def test_review_source_cache_is_dropped_when_the_account_changes(
    desktop_window, monkeypatch, qtbot
):
    content = b"%PDF-1.7 account-bound cache fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    downloads = []

    def source(_self, _deck):
        downloads.append(_deck)
        return DownloadedPDF(content, str(deck_id), "revision-1")

    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck: record)
    monkeypatch.setattr(ReviewService, "source", source)
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    monkeypatch.setattr(
        review_widget, "render", lambda _content, _index: ({"sizes": [[100, 100]]}, image.copy())
    )
    window = _signed_in_review(desktop_window)
    review = window.review
    review.set_deck(deck_id, "Private deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)

    window.account.session = FakeSession(
        tokens=Tokens("other", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.account_id = "other-account"
    window.account.session_changed.emit()
    assert review._source_cache is None
    review.set_deck(deck_id, "Private deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)
    assert downloads == [deck_id, deck_id]


def test_review_revisited_pdf_pages_do_not_render_again(desktop_window, monkeypatch, qtbot):
    content = b"%PDF-1.7 page cache fixture"
    digest = hashlib.sha256(content).hexdigest()
    deck_id, payload = analysis_payload(source_sha256=digest)
    record = parse_analysis(payload, deck_id)
    monkeypatch.setattr(ReviewService, "analysis", lambda _self, _deck: record)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda _self, _deck: DownloadedPDF(content, str(deck_id), "revision-1"),
    )
    renders = []

    def render(_content, index):
        renders.append(index)
        image = QImage(100, 100, QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        return {"sizes": [[100, 100]] * 3}, image

    monkeypatch.setattr(review_widget, "render", render)
    review = _signed_in_review(desktop_window).review
    review.set_deck(deck_id, "Paged deck")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy and review.pdf_content is not None)

    review.show_page(1)
    qtbot.waitUntil(lambda: not review.runner.busy and review.page_index == 1)
    review.show_page(0)
    assert review.page_index == 0 and not review.runner.busy
    review.show_page(1)
    assert review.page_index == 1 and not review.runner.busy
    assert renders == [0, 1]


def test_pdf_review_retries_versioned_contract_on_upgrade(monkeypatch):
    from buddy_desktop.config import AppConfig
    from buddy_desktop.review_service import PDFAnalysisUpgradeRequired, ReviewService

    service = ReviewService(AppConfig("http://127.0.0.1:8000"), "token")
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise PDFAnalysisUpgradeRequired("upgrade")
        return b"{}", None

    monkeypatch.setattr(service, "_get", get)
    monkeypatch.setattr(
        "buddy_desktop.review_service.parse_analysis",
        lambda *_: SimpleNamespace(source_format="pdf"),
    )
    deck = uuid4()
    assert service.analysis(deck).source_format == "pdf"
    assert calls == [f"/api/v1/analyses/by-deck/{deck}", f"/api/v2/analyses/pdf/by-deck/{deck}"]


def test_visual_qa_accepts_successful_response_after_default_timeout(monkeypatch):
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from buddy_desktop.config import AppConfig
    from buddy_desktop.review_service import ReviewService

    deck_id, payload = analysis_payload()
    analysis = parse_analysis(payload, deck_id)
    action = analysis.slides[0].actions[0]
    payload_bytes = json.dumps(payload).encode()
    request_started = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request_started.set()
            time.sleep(5.2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = ReviewService(AppConfig(f"http://127.0.0.1:{server.server_port}"), "token")
    try:
        updated = service.visual_qa(analysis, action)
        assert request_started.is_set()
        assert updated.id == analysis.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_review_parser_keeps_pdf_grounded_slide_text_for_spoken_commands():
    # #239: "highlight 48 million" must match the PDF action that shows "$48M".
    deck_id, payload = analysis_payload()
    payload["data"]["inputs"]["grounding"] = [
        {
            "instance_id": "action-1",
            "action_sha256": "c" * 64,
            "trace": {"method": "pdf_words", "target_text": "Revenue  $48M"},
        },
        "malformed entry is skipped",
    ]
    result = parse_analysis(payload, deck_id)
    assert result.slides[0].actions[0].shown_text == "Revenue $48M"
    deck_id, payload = analysis_payload()
    assert parse_analysis(payload, deck_id).slides[0].actions[0].shown_text is None
