"""Opt-in generated-input policy smoke on the real PDF player and Windows Office.

No microphone or providers. Eligibility and versioned session transport use a local
file-backed test double; reports explicitly exclude backend and model acceptance.
Only a generated temporary PowerPoint deck is opened/closed. Existing decks are untouched.
"""

import argparse
import hashlib
import importlib
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QRectF, QSettings
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from buddy_desktop.config import AppConfig
from buddy_desktop.powerpoint_contract import canonical_path
from buddy_desktop.powerpoint_present import PowerPointPresent
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import PresentationSession, SessionState
from buddy_desktop.review_service import ReviewAnalysis, ReviewSlide, ReviewSummary
from buddy_desktop.transcript_context import ContextSegment, ContextSnapshot
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Requires interactive Windows and installed desktop PowerPoint")
    args.output.mkdir(parents=True, exist_ok=False)
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    office = client.Dispatch("PowerPoint.Application")
    office.Visible = True
    app = QApplication([])
    apply_theme(app)
    window = MainWindow(
        AppConfig("http://127.0.0.1:1", 0.5),
        QSettings(str(args.output / "settings.ini"), QSettings.Format.IniFormat),
    )
    window.setWindowTitle("Buddy - scripted transition measurement")
    window.resize(1280, 900)
    window.show()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._agent_tick.stop()
    page.session_events.bind = lambda *args: None
    events, saves, results = [], [], []
    page.transition_observed.connect(events.append)
    original_consider = page.policy.consider

    def considered(*args, **kwargs):
        reason = original_consider(*args, **kwargs)
        print(json.dumps({"admission": reason}), flush=True)
        return reason

    page.policy.consider = considered
    native_path = (args.output / "scripted-policy.pptx").resolve()

    def wait(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            pythoncom.PumpWaitingMessages()
            if time.monotonic() > deadline:
                raise AssertionError(
                    "Scripted app condition timed out: " + page.policy_status.text()
                )
            time.sleep(0.01)
        app.processEvents()

    def save(access, previous, state):
        saves.append(asdict(state))
        (args.output / "test-session-states.json").write_text(
            json.dumps(saves, indent=2), encoding="utf-8"
        )
        return replace(previous, state=state, state_version=previous.state_version + 1)

    page.session_service.update = save
    page.agent.service.check_transition = lambda access, session, target: {
        "session_id": session.id,
        "analysis_id": session.analysis_version_id,
        "state_version": session.state_version,
        "target": target,
    }
    try:
        pdf = args.output / "scripted-policy.pdf"
        writer = QPdfWriter(str(pdf))
        painter = QPainter(writer)
        for i in range(2):
            if i:
                writer.newPage()
            painter.drawText(QRectF(100, 100, 2000, 500), "Scripted policy slide " + str(i + 1))
        painter.end()
        del writer
        for source_format in ("pdf", "pptx"):
            if source_format == "pdf":
                page.load_pdf(pdf)
                wait(lambda: not page.executor.busy and page.executor.manifest is not None)
                manifest = page.executor.manifest
                slide_ids = [s.slide_id for s in manifest.slides]
                deck_id, analysis_id = manifest.deck_id, str(uuid4())
            else:
                deck = office.Presentations.Add(-1)
                for i in range(1, 3):
                    deck.Slides.Add(i, 12).Shapes.AddTextbox(
                        1, 30, 30, 400, 80
                    ).TextFrame.TextRange.Text = "Scripted policy slide " + str(i)
                native_ids = [deck.Slides.Item(i).SlideID for i in (1, 2)]
                deck.SlideShowSettings.ShowType = 2
                deck.SaveAs(str(native_path), 24)
                deck.Close()
                page._submit_powerpoint("launch", str(native_path))
                wait(lambda: page._powerpoint_info is not None and not page.powerpoint_runner.busy)
                info = page._powerpoint_info
                content = native_path.read_bytes()
                summary = ReviewSummary("available", "Generated script", None)
                analysis = ReviewAnalysis(
                    uuid4(),
                    uuid4(),
                    1,
                    "published",
                    False,
                    "scripted-1",
                    hashlib.sha256(content).hexdigest(),
                    summary,
                    tuple(
                        ReviewSlide(
                            "script-" + str(i),
                            info.slide_width,
                            info.slide_height,
                            summary,
                            (),
                            native_slide_id=native_ids[i - 1],
                        )
                        for i in (1, 2)
                    ),
                    "pptx",
                    "scripted-render",
                    True,
                )
                binding = PowerPointSessionBinding.create(analysis, content, info)
                page._powerpoint_binding = binding
                page._native_present = PowerPointPresent(binding)
                slide_ids = [s.slide_id for s in binding.slides]
                deck_id, analysis_id = binding.deck_id, binding.analysis_id
                page._powerpoint_poll.stop()
            session = PresentationSession(
                str(uuid4()),
                str(uuid4()),
                deck_id,
                analysis_id,
                "active",
                1,
                SessionState(slide_ids[0], autonomy_mode="autonomous"),
                datetime.now(UTC),
            )
            page._session, page._session_access = session, "scripted-local-test"
            page._session_pending_state = None
            page._context_connected = True
            page.microphone._enabled = True
            page.agent_enabled.setChecked(True)
            page.policy_enabled.setChecked(True)
            wait(lambda: not page.session_runner.busy and page._session_pending_state is None)
            wait(lambda: time.monotonic_ns() >= page.policy._hold_until)
            session = page._session
            now = time.monotonic_ns()
            context = ContextSnapshot(
                session.id,
                session.state_version,
                2,
                str(uuid4()),
                "en-US",
                (ContextSegment("script", 1, "Please go to the next slide.", True, 0, 1, now),),
                now,
                True,
            )
            page.transcript_context.snapshot = lambda context=context: context
            proposal = {
                "decision_id": str(uuid4()),
                "session_id": session.id,
                "analysis_id": analysis_id,
                "state_version": session.state_version,
                "context_epoch": 2,
                "stream_id": context.stream_id,
                "sequence": 1,
                "confidence": 1.0,
                "decision_source": "navigation",
                "reason": "explicit",
                "evidence": [0],
                "target": {
                    "tool": "next_slide",
                    "slide_id": slide_ids[1],
                    "action_id": None,
                    "region": None,
                },
            }
            count = len(events)
            started = time.monotonic_ns()
            page._agent_proposed(proposal)
            wait(lambda count=count: len(events) == count + 1)
            wait(lambda: not page.session_runner.busy and page._session_pending_state is None)
            elapsed = (events[-1]["completed_ns"] - started) / 1e6
            assert page._current_local_state().active_slide_id == slide_ids[1]
            assert saves[-1]["active_slide_id"] == slide_ids[1]
            page._agent_proposed(proposal)
            app.processEvents()
            assert len(events) == count + 1
            undo_started = time.monotonic_ns()
            page.policy_undo_button.click()
            wait(
                lambda slide_ids=slide_ids: (
                    page._current_local_state().active_slide_id == slide_ids[0]
                    and not page.powerpoint_runner.busy
                    and not page.executor.busy
                )
            )
            wait(lambda: not page.session_runner.busy and page._session_pending_state is None)
            assert saves[-1]["active_slide_id"] == slide_ids[0]
            results.append(
                {
                    "source_format": source_format,
                    "navigation_ms": elapsed,
                    "undo_ms": (time.monotonic_ns() - undo_started) / 1e6,
                    "duplicate_suppressed": True,
                    "observed_state_saved": True,
                    "animation_observed": events[-1]["animated"],
                }
            )
            page.policy_enabled.setChecked(False)
            page.microphone._enabled = False
        report = {
            "scope": "scripted_live_players",
            "input_kind": "generated",
            "office_runtime": str(office.Version) + "." + str(office.Build),
            "substituted_boundaries": ["speech input", "eligibility HTTP", "session transport"],
            "model_accuracy_measured": False,
            "independent_holdout": False,
            "results": results,
        }
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (args.output / "player-events.json").write_text(
            json.dumps(events, indent=2, default=lambda value: asdict(value)), encoding="utf-8"
        )
        print(json.dumps(report, indent=2), flush=True)
    finally:
        page.shutdown()
        page.powerpoint_runner.wait_for_shutdown()
        page.session_runner.wait_for_shutdown()
        page.policy_runner.wait_for_shutdown()
        window.close()
        for i in range(office.Presentations.Count, 0, -1):
            deck = office.Presentations.Item(i)
            if canonical_path(deck.FullName) == canonical_path(native_path):
                deck.Saved = True
                deck.Close()
        pythoncom.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
