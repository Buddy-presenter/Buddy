"""Opt-in #153 saved-action UI acceptance on real PDFium and Windows PowerPoint.

Uses generated slides and explicit synthetic analysis/eligibility and session transports.
No microphone, paid provider, production API or existing user presentation is used.
This proves native execution/recovery, not server publication or semantic verification.
"""

import argparse
import hashlib
import importlib
import json
import platform
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSettings
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from buddy_desktop.config import AppConfig
from buddy_desktop.powerpoint import PowerPointAutomation
from buddy_desktop.powerpoint_contract import PowerPointAction, canonical_path
from buddy_desktop.powerpoint_generated import SAVED_ACTION_TYPES
from buddy_desktop.powerpoint_present import PowerPointPresent
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.review_service import (
    ReviewAction,
    ReviewAnalysis,
    ReviewService,
    ReviewSlide,
    ReviewSummary,
)
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "win32" or PowerPointAutomation._powerpoint_process_running():
        parser.error(
            "Use an interactive Windows/Office session without existing PowerPoint processes."
        )
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    pythoncom, client = (
        importlib.import_module("pythoncom"),
        importlib.import_module("win32com.client"),
    )
    pythoncom.CoInitialize()
    office = client.Dispatch("PowerPoint.Application")
    office.Visible = True
    runtime = f"{office.Version}.{office.Build}"
    app = QApplication([])
    apply_theme(app)
    window = MainWindow(
        AppConfig("http://127.0.0.1:1", 0.5),
        QSettings(str(args.output / "settings.ini"), QSettings.Format.IniFormat),
    )
    window.setWindowTitle("Buddy — synthetic saved-action acceptance")
    window.resize(1200, 800)
    window.show()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    page.session_events.bind = lambda *args: None
    page._ensure_session = lambda **kwargs: None
    original_read = ReviewService.analysis
    current_analysis, writes, results = [], [], []
    native_path = args.output / "synthetic-saved-actions.pptx"
    deck = None

    def wait(predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            pythoncom.PumpWaitingMessages()
            if time.monotonic() > deadline:
                raise AssertionError(
                    "Timed out: "
                    + page.generated_status.text()
                    + " / "
                    + str(
                        {
                            "session": page._session,
                            "pending": page._session_pending_state,
                            "followup": page._session_followup,
                            "restore": page._session_restore_state,
                            "native_busy": page.powerpoint_runner.busy,
                            "session_busy": page.session_runner.busy,
                            "verified": page._powerpoint_verified,
                            "writes": len(writes),
                        }
                    )
                )
            time.sleep(0.01)

    def idle():
        return (
            page._session_pending_state is None
            and page._session_followup is None
            and not (
                page.generated_runner.busy
                or page.powerpoint_runner.busy
                or page.executor.busy
                or page.session_runner.busy
            )
        )

    def save(access, previous, state):
        writes.append(asdict(state))
        return replace(previous, state=state, state_version=previous.state_version + 1)

    page.session_service.update = save
    ReviewService.analysis = lambda service, identifier, source_format: current_analysis[0]
    try:
        for fmt in ("pdf", "pptx"):
            page.clear_session_binding()
            wait(idle)
            deck_id, analysis_id = uuid4(), uuid4()
            summary = ReviewSummary("available", "Synthetic quarterly results", None)
            shape_id = None
            if fmt == "pdf":
                pdf = args.output / "synthetic-saved-actions.pdf"
                writer = QPdfWriter(str(pdf))
                painter = QPainter(writer)
                painter.drawText(600, 1000, "Synthetic quarterly results: 48 million")
                painter.end()
                del writer
                page.load_pdf(pdf)
                wait(lambda: not page.executor.busy and page.executor.manifest is not None)
                manifest = page.executor.manifest
                slide = manifest.slides[0]
                width, height, slide_id, native_id = slide.width, slide.height, slide.slide_id, 0
                source = pdf.read_bytes()
                region = (width * 0.1, height * 0.1, width * 0.4, height * 0.2)
            else:
                page.executor.close()
                deck = office.Presentations.Add()
                deck.PageSetup.SlideWidth, deck.PageSetup.SlideHeight = 960, 540
                slide = deck.Slides.Add(1, 12)
                shape = slide.Shapes.AddTextbox(1, 160, 180, 360, 90)
                shape.TextFrame.TextRange.Text = "Synthetic quarterly results: 48 million"
                shape.TextFrame.TextRange.Font.Size = 28
                shape_id = int(shape.Id)
                deck.SaveAs(str(native_path))
                deck.SlideShowSettings.Run()
                info = page.powerpoint.connect(native_path)
                page._powerpoint_info, page._powerpoint_verified = info, True
                page.powerpoint_controls.set_info(info)
                width, height, slide_id, native_id = 960, 540, "slide-1", int(slide.SlideID)
                source = native_path.read_bytes()
                region = tuple(
                    float(value) for value in (shape.Left, shape.Top, shape.Width, shape.Height)
                )
            kinds = (
                ("highlight_region", "zoom_region") if fmt == "pdf" else sorted(SAVED_ACTION_TYPES)
            )
            actions = []
            for kind in kinds:
                raw = {
                    "version": "powerpoint-2",
                    "source_format": fmt,
                    "deck_id": str(deck_id),
                    "render_id": "synthetic-render",
                    "slide_id": slide_id,
                    "instance_id": "synthetic-" + kind,
                    "type": kind,
                    "purpose": "Synthetic " + kind.replace("_", " "),
                    "params": {
                        "region": dict(zip(("x", "y", "width", "height"), region, strict=True)),
                        "text": "Measured results" if kind in {"callout", "text_box"} else "",
                    },
                }
                if kind == "zoom_region" and fmt == "pptx":
                    raw["params"]["source_shape_id"] = shape_id
                digest = hashlib.sha256(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                actions.append(
                    ReviewAction(
                        instance_id=raw["instance_id"],
                        purpose=raw["purpose"],
                        action_type=kind,
                        version=raw["version"],
                        region=region,
                        state="validated",
                        usable=True,
                        validation_kind="visual",
                        semantic_verified=True,
                        evidence=(),
                        reasons=(),
                        profile="synthetic",
                        validator_version="synthetic-eligibility-fixture",
                        renderer_version="synthetic",
                        validation_id=None,
                        raw=raw,
                        runtime_version=runtime if fmt == "pptx" else None,
                        action_sha256=digest,
                    )
                )
            analysis = ReviewAnalysis(
                analysis_id,
                deck_id,
                1,
                "published",
                False,
                "synthetic-revision",
                hashlib.sha256(source).hexdigest(),
                summary,
                (ReviewSlide(slide_id, width, height, summary, tuple(actions), native_id),),
                fmt,
                "synthetic-render",
                fmt == "pptx",
            )
            current_analysis[:] = [analysis]
            if fmt == "pptx":
                binding = PowerPointSessionBinding.create(analysis, source, info)
                page._powerpoint_binding = binding
                page._native_present = PowerPointPresent(binding)
            page._session = PresentationSession(
                str(uuid4()),
                str(uuid4()),
                str(deck_id),
                str(analysis_id),
                "active",
                1,
                SessionState(slide_id),
                datetime.now(UTC),
            )
            page._session_access = lambda: "synthetic-local-owner"
            page._generated_autoload = str(analysis_id)
            page._refresh_generated()
            page.generated_reload.click()
            wait(idle)
            before = int(slide.Shapes.Count) if fmt == "pptx" else None
            for action in actions:
                page.generated_actions.setCurrentIndex(
                    page.generated_actions.findData(action.instance_id)
                )
                wait(lambda: page.generated_apply.isEnabled())
                previous = len(writes)
                print(
                    "APPLY",
                    fmt,
                    action.action_type,
                    "version",
                    page._session.state_version,
                    flush=True,
                )
                page.generated_apply.click()
                wait(idle)
                if page._accepted_action_id != action.instance_id:
                    message = page.generated_status.text()
                    if (
                        fmt == "pptx"
                        and action.action_type == "zoom_region"
                        and "lost focus" in message
                    ):
                        page.powerpoint.execute(
                            PowerPointAction("reset_zoom", page._powerpoint_info)
                        )
                        results.append(
                            {
                                "format": fmt,
                                "type": action.action_type,
                                "applied": False,
                                "reset_restored": True,
                                "status": "blocked",
                                "reason": message,
                            }
                        )
                        continue
                    raise AssertionError("Saved action did not complete: " + message)
                assert len(writes) > previous
                if fmt == "pdf":
                    assert (
                        page.surface.highlight is not None
                        if action.action_type == "highlight_region"
                        else page.surface.region != slide.full
                    )
                    page.send("reset_view")
                    wait(idle)
                    assert page.surface.highlight is None and page.surface.region == slide.full
                else:
                    if action.action_type != "zoom_region":
                        assert slide.Shapes.Count > before
                        info = page.powerpoint.execute(
                            PowerPointAction("undo_annotation", page._powerpoint_info)
                        ).info
                        assert slide.Shapes.Count == before
                    info = page.powerpoint.execute(
                        PowerPointAction("reset_view", page._powerpoint_info)
                    ).info
                    assert slide.Shapes.Count == before
                    page._native_present.focus = None
                    page._powerpoint_info = info
                results.append(
                    {
                        "format": fmt,
                        "type": action.action_type,
                        "applied": True,
                        "reset_restored": True,
                        "saved_action_id_confirmed": True,
                    }
                )
            window.pages.currentWidget().ensureWidgetVisible(page.generated_actions)
            app.processEvents()
            window.grab().save(str(args.output / f"{fmt}-controls.png"))
        report = {
            "scope": "synthetic_saved_action_executor",
            "os": platform.platform(),
            "office_runtime": runtime,
            "source_sha256": {
                name: hashlib.sha256(
                    (Path(__file__).resolve().parents[1] / "src/buddy_desktop" / name).read_bytes()
                ).hexdigest()
                for name in (
                    "present_widget.py",
                    "powerpoint_present.py",
                    "powerpoint_generated.py",
                )
            },
            "substituted_boundaries": ["analysis/eligibility HTTP", "session transport"],
            "server_publication_verified": False,
            "semantic_verification_measured": False,
            "speech_or_model_accuracy_measured": False,
            "results": results,
            "complete": all(row["applied"] for row in results),
        }
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    finally:
        ReviewService.analysis = original_read
        page.shutdown()
        for runner in (
            page.powerpoint_runner,
            page.session_runner,
            page.generated_runner,
            page.policy_runner,
        ):
            runner.wait_for_shutdown()
        window.close()
        if deck is not None and canonical_path(deck.FullName) == canonical_path(native_path):
            deck.Saved = True
            deck.Close()
        office.Quit()
        pythoncom.CoUninitialize()
    return 0 if all(row["applied"] for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
