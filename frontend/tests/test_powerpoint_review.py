import hashlib
import json
import time
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from frontend.tests.test_review import analysis_payload
from PySide6.QtGui import QColor, QImage

import buddy_desktop.review_widget as widgets
from buddy_desktop.auth import Tokens
from buddy_desktop.powerpoint_contract import PresentationInfo
from buddy_desktop.powerpoint_generated import generated_action
from buddy_desktop.review_service import DownloadedPowerPoint, ReviewService, parse_analysis
from buddy_desktop.services import ServiceError

KINDS = (
    "highlight_region",
    "draw_rectangle",
    "draw_oval",
    "draw_underline",
    "draw_arrow",
    "callout",
    "draw_line",
    "filled_rectangle",
    "filled_oval",
    "text_box",
    "draw_rounded_rectangle",
    "draw_diamond",
    "draw_triangle",
    "draw_hexagon",
    "draw_star",
)


def powerpoint_payload(content=b"PK-synthetic-source", *, usable=True, kind="highlight_region"):
    deck, payload = analysis_payload(source_sha256=hashlib.sha256(content).hexdigest())
    data, inputs = payload["data"], payload["data"]["inputs"]
    data["source_format"] = "pptx"
    data["publication"] = "published"
    inputs.update(
        source_format="pptx",
        schema_version="buddy.powerpoint.analysis.v2",
        catalog_version="powerpoint-2",
        office_safe=True,
        display_profile="windows-powerpoint-2",
        validator_version="powerpoint-executor-2",
        renderer_version="desktop-powerpoint-2",
    )
    manifest = inputs["manifest"]
    manifest.update(source_format="pptx", version="powerpoint-2")
    manifest["slides"][0].update(
        native_slide_id=256,
        source={"width": 960, "height": 540, "units": "slide_points", "origin": "top_left"},
    )
    manifest["slides"][0].pop("raster")
    result = data["slides"][0]["actions"][0]
    action = result["action"]
    action.update(source_format="pptx", version="powerpoint-2", type=kind)
    action["params"]["text"] = "Measured results" if kind in {"callout", "text_box"} else ""
    state = "validated" if usable else "needs-review"
    result.update(
        state=state,
        usable=usable,
        validation={
            "source_format": "pptx",
            "schema_version": "buddy.validation.v1",
            "validation_id": str(uuid4()),
            "analysis_id": data["id"],
            "analysis_sha256": data["analysis_sha256"],
            "instance_id": action["instance_id"],
            "action_sha256": hashlib.sha256(
                json.dumps(action, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "state": state,
            "reasons": [] if usable else ["powerpoint_runtime_requires_windows_office"],
            "evidence": [{"id": str(uuid4()), "kind": "report", "sha256": "b" * 64}],
            "runtime_version": "16.0.12345",
            "validator_version": "powerpoint-executor-2",
            "renderer_version": "desktop-powerpoint-2",
            "display_profile": "windows-powerpoint-2",
        },
    )
    return deck, payload


@pytest.mark.parametrize("kind", KINDS)
def test_powerpoint_review_all_actions_map_to_the_typed_real_executor(kind):
    deck, payload = powerpoint_payload(kind=kind)
    analysis = parse_analysis(payload, deck)
    slide, action = analysis.slides[0], analysis.slides[0].actions[0]
    assert analysis.source_format == "pptx" and slide.native_slide_id == 256
    assert action.usable and action.runtime_version == "16.0.12345"
    target = PresentationInfo("test.pptx", "test.pptx", 1, 1, 123, 256, 960, 540)
    command = generated_action(
        action.raw,
        target,
        deck_id=str(deck),
        render_id=analysis.render_id,
        slide_id=slide.slide_id,
        native_slide_id=256,
        width=960,
        height=540,
    )
    assert command.kind == kind
    command.validate()
    if kind in {"draw_arrow", "draw_line"}:
        assert command.points == ((10, 20), (40, 60)) and command.region is None
    else:
        assert command.region.width == 30


@pytest.mark.parametrize(
    "change",
    [
        "pdf_input",
        "pdf_action",
        "missing_runtime",
        "missing_validation",
        "wrong_profile",
        "unvalidated_usable",
        "pixel_plane",
        "missing_native_id",
        "hash_changed",
    ],
)
def test_powerpoint_review_rejects_mixed_formats_and_false_validation_claims(change):
    deck, payload = powerpoint_payload()
    data = payload["data"]
    item = data["slides"][0]["actions"][0]
    if change == "pdf_input":
        data["inputs"]["source_format"] = "pdf"
    elif change == "pdf_action":
        item["action"]["source_format"], item["action"]["version"] = "pdf", "1.1"
    elif change == "missing_runtime":
        item["validation"]["runtime_version"] = None
    elif change == "missing_validation":
        item["validation"] = None
    elif change == "wrong_profile":
        data["inputs"]["display_profile"] = item["validation"]["display_profile"] = "wide-1x"
    elif change == "unvalidated_usable":
        item["state"] = item["validation"]["state"] = "needs-review"
    elif change == "pixel_plane":
        data["inputs"]["manifest"]["slides"][0]["source"]["units"] = "source_pixels"
    elif change == "missing_native_id":
        data["inputs"]["manifest"]["slides"][0].pop("native_slide_id")
    else:
        item["action"]["params"]["region"]["x"] += 1
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_analysis(payload, deck)


def test_pdf_review_rejects_powerpoint_coordinates():
    deck, payload = analysis_payload()
    payload["data"]["inputs"]["manifest"]["slides"][0]["source"]["units"] = "slide_points"
    with pytest.raises(ValueError):
        parse_analysis(payload, deck)


def test_pptx_review_is_explicit_and_clears_private_session(desktop_window, monkeypatch, qtbot):
    content = b"PK-synthetic-source"
    deck, payload = powerpoint_payload(content)
    analysis = parse_analysis(payload, deck)
    monkeypatch.setattr(
        ReviewService,
        "analysis",
        lambda _self, _id, source_format: analysis if source_format == "pptx" else None,
    )
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda _self, _id, source_format: DownloadedPowerPoint(content, str(deck), "revision-1"),
    )
    calls = []

    # External Office boundary; the actual Office executor has a separate opt-in matrix.
    class Session:
        def __init__(self):
            self.cancelled = Event()

        def open(self, raw, saved, index):
            assert raw == content and saved.source_format == "pptx"
            calls.append("open")
            image = QImage(1280, 720, QImage.Format.Format_RGB32)
            image.fill(QColor("white"))
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice

            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            return SimpleNamespace(png=bytes(data))

        def apply(self, action, saved, index):
            assert action.usable
            calls.append("apply")
            return self.open(content, saved, index)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(widgets, "PowerPointReviewSession", Session)
    monkeypatch.setattr(widgets, "sys", SimpleNamespace(platform="win32"))
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck, "Quarterly results.pptx", "review_ready", "pptx")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert calls == [] and review.open_powerpoint.isEnabled()
    assert not review.run_powerpoint.isEnabled()
    assert "PowerPoint slide points" in review.action_detail.toPlainText()
    review.open_powerpoint.click()
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert review.run_powerpoint.isEnabled() and calls == ["open"]
    assert not review.image.isNull()
    review.run_powerpoint.click()
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert calls[:2] == ["open", "apply"]
    assert "action completed" in review.notice.text()
    window.account.session.tokens = None
    window.account.session_changed.emit()
    assert review.pdf_content is None and review.analysis is None and review._pptx_session is None
    qtbot.waitUntil(lambda: not review.cleanup_runner.busy)
    assert calls[-1] == "close"


def test_powerpoint_runtime_failure_remains_inspectable(desktop_window, monkeypatch, qtbot):
    content = b"PK-synthetic-source"
    deck, payload = powerpoint_payload(content, usable=False)
    analysis = parse_analysis(payload, deck)
    monkeypatch.setattr(ReviewService, "analysis", lambda *_args: analysis)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda *_args: DownloadedPowerPoint(content, str(deck), "revision-1"),
    )
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck, "Review source.pptx", "review_ready", "pptx")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert "runtime_requires_windows_office" in review.action_detail.toPlainText()
    assert not review.run_powerpoint.isEnabled()
    assert review.analysis.presentation_summary.text == "Overall summary"


@pytest.mark.parametrize("width,height", [(800, 600), (1360, 1000)])
def test_powerpoint_review_visual_layout(
    desktop_window, monkeypatch, qtbot, qapp, request, width, height
):
    import os
    from pathlib import Path

    from buddy_desktop.visuals import apply_theme

    original_style, original_palette, original_font = qapp.styleSheet(), qapp.palette(), qapp.font()

    def restore_theme():
        qapp.setStyleSheet(original_style)
        qapp.setPalette(original_palette)
        qapp.setFont(original_font)

    request.addfinalizer(restore_theme)
    apply_theme(qapp)

    content = b"PK-synthetic-source"
    deck, payload = powerpoint_payload(content, usable=False)
    analysis = parse_analysis(payload, deck)
    monkeypatch.setattr(ReviewService, "analysis", lambda *_args: analysis)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda *_args: DownloadedPowerPoint(content, str(deck), "revision-1"),
    )
    window = desktop_window()
    window.resize(width, height)
    window.account.session = SimpleNamespace(
        tokens=Tokens("synthetic-token", "synthetic-refresh", time.monotonic() + 3600),
        cancelled=Event(),
    )
    window.account.session_changed.emit()
    window.navigation.setCurrentRow(2)
    window.review.set_deck(deck, "Quarterly results.pptx", "review_ready", "pptx")
    qtbot.waitUntil(lambda: not window.review.runner.busy)
    assert not window.review.content.isHidden()
    assert window.review.open_powerpoint.geometry().right() < window.review.content.width()
    assert window.review.run_powerpoint.geometry().right() < window.review.content.width()
    assert (
        window.review.run_powerpoint.geometry().top()
        > window.review.open_powerpoint.geometry().bottom()
    )
    window.review.open_powerpoint.setFocus()
    qtbot.wait(20)
    evidence = os.environ.get("BUDDY_VISUAL_EVIDENCE_DIR")
    if evidence:
        folder = Path(evidence)
        folder.mkdir(parents=True, exist_ok=True)
        image = folder.parent / "office/960-highlight_region/before_image.png"
        if image.is_file():
            window.review.image = QImage(str(image))
            window.review._display_page()
            qtbot.wait(50)
        assert window.grab().save(str(folder / f"powerpoint-review-{width}x{height}.png"))


def test_real_office_indexed_png_is_decoded_and_invalid_previews_are_rejected(qapp):
    from pathlib import Path

    from buddy_desktop.review_widget import powerpoint_image

    png = (Path(__file__).parent / "fixtures/powerpoint/indexed-preview.png").read_bytes()
    image = powerpoint_image(png)
    assert not image.isNull() and (image.width(), image.height()) == (1280, 720)
    with pytest.raises(ServiceError):
        powerpoint_image(b"not a PNG")


def test_private_review_reopens_an_ended_show_and_rechecks_source(monkeypatch):
    import buddy_desktop.powerpoint_review as sessions
    from buddy_desktop.powerpoint_contract import PowerPointError, PowerPointFrame

    content = b"PK-synthetic-source"
    deck, payload = powerpoint_payload(content)
    analysis = parse_analysis(payload, deck)
    info = PresentationInfo("test.pptx", "test.pptx", 1, 1, 123, 256, 960, 540)

    class Automation:
        launches = 0
        current = None

        def launch(self, path):
            self.launches += 1
            self.current = info

        def status(self, path):
            return self.current

        def preview(self, target):
            return PowerPointFrame(target, b"preview")

        def _process_probe(self):
            return False

    automation = Automation()
    monkeypatch.setattr(sessions, "PowerPointAutomation", lambda: automation)
    session = sessions.PowerPointReviewSession()
    try:
        session.open(content, analysis, 0)
        automation.current = None  # Presenter ended the external slideshow.
        session.open(content, analysis, 0)
        assert automation.launches == 2
        session.path.write_bytes(b"modified-source")
        with pytest.raises(PowerPointError, match="source has changed"):
            session.open(content, analysis, 0)
        assert automation.launches == 2
    finally:
        session.close()


def test_pending_powerpoint_review_does_not_download_the_source(desktop_window, monkeypatch, qtbot):
    def pending(*_args):
        raise ServiceError("No saved analysis is available for this presentation yet.")

    def unexpected_download(*_args):
        pytest.fail("A pending PowerPoint analysis must not trigger a source download")

    monkeypatch.setattr(ReviewService, "analysis", pending)
    monkeypatch.setattr(ReviewService, "source", unexpected_download)
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(uuid4(), "Still processing.pptx", "processing", "pptx")
    review.activate(True)
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert "processing is still running" in review.notice.text()
    assert not review.content.isHidden()
    assert not review.open_powerpoint.isEnabled()


def test_powerpoint_source_download_uses_a_transfer_timeout(monkeypatch):
    import buddy_desktop.review_service as transport
    from buddy_desktop.config import AppConfig

    timeouts = []

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            raise OSError("synthetic network failure")

    monkeypatch.setattr(transport, "build_opener", lambda *_args: Opener())
    service = ReviewService(AppConfig("http://127.0.0.1:8000", 5), "token")
    with pytest.raises(ServiceError):
        service.source(uuid4(), "pptx")
    with pytest.raises(ServiceError):
        service.analysis(uuid4(), "pptx")
    assert timeouts == [120, 5]


def test_action_details_are_opt_in_and_evidence_png_opens(desktop_window, monkeypatch, qtbot):
    from pathlib import Path

    content = b"PK-synthetic-source"
    deck, payload = powerpoint_payload(content)
    analysis = parse_analysis(payload, deck)
    monkeypatch.setattr(ReviewService, "analysis", lambda *_args: analysis)
    monkeypatch.setattr(
        ReviewService,
        "source",
        lambda *_args: DownloadedPowerPoint(content, str(deck), "revision-1"),
    )
    window = desktop_window()
    window.account.session = SimpleNamespace(
        tokens=Tokens("token", "refresh", time.monotonic() + 3600), cancelled=Event()
    )
    window.account.session_changed.emit()
    review = window.review
    review.set_deck(deck, "Quarterly results.pptx", "review_ready", "pptx")
    window.navigation.setCurrentRow(2)
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert review.details_panel.isHidden()
    button = review.action_table.cellWidget(0, 3)
    qtbot.mouseClick(button, widgets.Qt.MouseButton.LeftButton)
    assert not review.details_panel.isHidden()
    assert "Validation record" in review.action_detail.toPlainText()
    png = (Path(__file__).parent / "fixtures/powerpoint/indexed-preview.png").read_bytes()
    monkeypatch.setattr(ReviewService, "evidence", lambda *_args: (png, "image/png"))
    review.open_evidence.click()
    qtbot.waitUntil(lambda: not review.runner.busy)
    assert review._evidence_dialog is not None and review._evidence_dialog.isVisible()
    review.clear_private()
    assert review._evidence_dialog is None


def test_annotation_layout_uses_clear_space_and_refuses_crowded_slides():
    from buddy_desktop.powerpoint_contract import PowerPointError
    from buddy_desktop.powerpoint_layout import annotation_box
    from buddy_desktop.slide_contract import Region

    target = Region(50, 50, 300, 80)
    occupied = (target, Region(50, 180, 400, 160))
    box = annotation_box(target, 720, 540, "Revenue grew 12%", occupied)
    assert box.x >= 0 and box.y >= 0 and box.x + box.width <= 720 and box.y + box.height <= 540
    for obj in occupied:
        assert (
            box.x + box.width <= obj.x
            or obj.x + obj.width <= box.x
            or box.y + box.height <= obj.y
            or obj.y + obj.height <= box.y
        )
    with pytest.raises(PowerPointError, match="No clear space"):
        annotation_box(target, 720, 540, "A takeaway", (Region(0, 0, 720, 540),))
