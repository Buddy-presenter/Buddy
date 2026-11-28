"""Manual presentation controls exercise the same production executor as generated actions."""

from pathlib import Path
from threading import Event
from time import monotonic_ns, sleep

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFileDialog

from buddy_desktop.slide_contract import Region

FIXTURES = Path(__file__).parent / "fixtures" / "slide_actions"


@pytest.fixture
def present(desktop_window, qtbot):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page.load_manifest(FIXTURES / "manifest.json")
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 1 of 3"
    return window, page


def region_fields(page, region):
    for name, value in region.items():
        page.fields[name].setValue(value)


def test_presenter_can_pause_and_resume_from_the_current_manual_slide(present, qtbot, monkeypatch):
    _window, page = present
    page._agent_tick.stop()
    monkeypatch.setattr(page, "_queue_session_state", lambda: None)
    page.policy_enabled.setEnabled(True)
    page.policy_pause_button.setEnabled(True)
    page.policy_pause_button.click()
    assert page.policy.enabled

    page.policy_pause_button.click()
    assert not page.policy.enabled
    assert "paused" in page.policy_status.text()
    page.next_button.click()
    qtbot.waitUntil(lambda: page.executor.current_index == 1 and not page.executor.busy)

    page.policy_enabled.setEnabled(True)
    page.policy_pause_button.setEnabled(True)
    page.policy_pause_button.click()
    assert page.policy.enabled
    assert page.executor.current_index == 1


def test_continued_speech_does_not_cancel_an_already_dispatched_office_action(present):
    _window, page = present
    page._native_guard.set()
    page._speech_transcript(
        {
            "final": False,
            "start_seconds": 0.0,
            "end_seconds": 0.1,
            "latency_ms": 10,
            "text": "and the next part",
        }
    )
    assert page._native_guard.is_set()


def test_transcript_update_runs_agent_immediately_without_waiting_for_timer(present, monkeypatch):
    _window, page = present
    from datetime import UTC, datetime
    from uuid import uuid4

    from buddy_desktop.presentation_session_service import PresentationSession, SessionState

    session_id = str(uuid4())
    page._session = PresentationSession(
        session_id,
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "active",
        1,
        SessionState("slide-1"),
        datetime.now(UTC),
    )
    page._context_connected = True
    calls = []
    monkeypatch.setattr(page, "_update_agent", lambda: calls.append("updated"))
    page._speech_transcript(
        {
            "session_id": session_id,
            "stream_id": "stream",
            "sequence": 1,
            "segment_id": "segment",
            "text": "Let's go to the next slide now",
            "final": False,
            "start_seconds": 0.0,
            "end_seconds": 0.8,
            "capture_end_ns": monotonic_ns(),
            "latency_ms": 10,
        }
    )
    assert calls == ["updated"]


def test_voice_search_stays_local_and_suppresses_partial_and_final_agent_input(
    present, signal_recorder, monkeypatch, qtbot
):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    page._agent_tick.stop()
    search_spy = QSignalSpy(page.web_search_requested)
    agent_updates = []
    monkeypatch.setattr(page, "_update_agent", lambda: agent_updates.append(True))
    base = {
        "start_seconds": 0.0,
        "end_seconds": 0.8,
        "latency_ms": 20,
        "segment_id": "search-command",
        "sequence": 1,
        "text": "Open Chrome and search for images of cute",
    }
    page._speech_transcript({**base, "final": False})
    assert search_spy.count() == 0
    page._speech_transcript(
        {**base, "text": "Open Chrome and search for images of cute cats.", "final": True}
    )
    assert search_spy.count() == 0
    qtbot.waitUntil(lambda: search_spy.count() == 1, timeout=4000)
    assert search_spy.count() == 1
    assert search_spy.at(0) == ["cute cats", True, True]
    assert agent_updates == []
    assert "Open Chrome and search" in page.speech_text.toPlainText()


def test_quick_search_correction_updates_pending_query_before_chrome_opens(
    present, signal_recorder, qtbot
):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    search_spy = QSignalSpy(page.web_search_requested)
    page._speech_transcript(
        {
            "text": "Open Chrome and search for images of dogs",
            "final": True,
            "segment_id": "first-search",
            "sequence": 1,
            "start_seconds": 0.0,
            "end_seconds": 0.7,
            "latency_ms": 20,
        }
    )
    page._speech_transcript(
        {
            "text": "I mean cute",
            "final": False,
            "segment_id": "search-correction",
            "sequence": 2,
            "start_seconds": 0.71,
            "end_seconds": 1.0,
            "latency_ms": 20,
        }
    )
    qtbot.wait(300)
    assert search_spy.count() == 0
    page._speech_transcript(
        {
            "text": "I mean cute cats",
            "final": True,
            "segment_id": "search-correction",
            "sequence": 3,
            "start_seconds": 0.71,
            "end_seconds": 1.2,
            "latency_ms": 20,
        }
    )

    assert search_spy.count() == 0
    qtbot.waitUntil(lambda: search_spy.count() == 1, timeout=4000)
    assert search_spy.at(0) == ["cute cats", True, True]


def test_incomplete_image_search_waits_for_a_concrete_query(present, signal_recorder, qtbot):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    search_spy = QSignalSpy(page.web_search_requested)
    page._speech_transcript(
        {
            "text": "Open Chrome and search for images of",
            "final": True,
            "segment_id": "incomplete-search",
            "sequence": 1,
            "start_seconds": 0.0,
            "end_seconds": 0.7,
            "latency_ms": 20,
        }
    )
    qtbot.wait(650)
    assert search_spy.count() == 0

    page._speech_transcript(
        {
            "text": "cute cats",
            "final": False,
            "segment_id": "search-subject",
            "sequence": 2,
            "start_seconds": 0.71,
            "end_seconds": 1.0,
            "latency_ms": 20,
        }
    )
    page._speech_transcript(
        {
            "text": "cute cats",
            "final": True,
            "segment_id": "search-subject",
            "sequence": 3,
            "start_seconds": 0.71,
            "end_seconds": 1.2,
            "latency_ms": 20,
        }
    )
    qtbot.waitUntil(lambda: search_spy.count() == 1, timeout=4000)
    assert search_spy.at(0) == ["cute cats", True, True]


def test_incomplete_correction_keeps_the_original_search_queued_until_query_arrives(
    present, signal_recorder, qtbot
):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    search_spy = QSignalSpy(page.web_search_requested)
    page._speech_transcript(
        {
            "text": "Open Chrome and search for images of dogs",
            "final": True,
            "segment_id": "initial-search",
            "sequence": 1,
            "start_seconds": 0.0,
            "end_seconds": 0.7,
            "latency_ms": 20,
        }
    )
    page._speech_transcript(
        {
            "text": "I mean",
            "final": True,
            "segment_id": "correction-marker",
            "sequence": 2,
            "start_seconds": 0.71,
            "end_seconds": 0.9,
            "latency_ms": 20,
        }
    )
    qtbot.wait(700)
    assert search_spy.count() == 0
    page._speech_transcript(
        {
            "text": "cute cats",
            "final": True,
            "segment_id": "correction-query",
            "sequence": 3,
            "start_seconds": 0.91,
            "end_seconds": 1.2,
            "latency_ms": 20,
        }
    )
    qtbot.waitUntil(lambda: search_spy.count() == 1, timeout=4000)
    assert search_spy.at(0) == ["cute cats", True, True]


def test_return_command_cancels_a_search_still_waiting_for_correction_window(
    present, signal_recorder
):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    search_spy = QSignalSpy(page.web_search_requested)
    return_spy = QSignalSpy(page.return_to_presentation_requested)
    page._speech_transcript(
        {
            "text": "Open Chrome and search for images of dogs",
            "final": True,
            "segment_id": "search-before-return",
            "sequence": 1,
            "start_seconds": 0.0,
            "end_seconds": 0.7,
            "latency_ms": 20,
        }
    )
    page._speech_transcript(
        {
            "text": "Go back to the PDF",
            "final": True,
            "segment_id": "return-before-search-open",
            "sequence": 2,
            "start_seconds": 0.71,
            "end_seconds": 1.2,
            "latency_ms": 20,
        }
    )
    assert search_spy.count() == 0
    assert return_spy.count() == 1


def test_voice_return_command_is_local_and_does_not_require_live_suggestions(
    present, signal_recorder, monkeypatch
):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    return_spy = QSignalSpy(page.return_to_presentation_requested)
    agent_updates = []
    monkeypatch.setattr(page, "_update_agent", lambda: agent_updates.append(True))
    page._speech_transcript(
        {
            "text": "Let's go back to the presentation.",
            "final": True,
            "segment_id": "return-command",
            "sequence": 2,
            "start_seconds": 1.0,
            "end_seconds": 1.8,
            "latency_ms": 20,
        }
    )
    assert return_spy.count() == 1
    assert return_spy.at(0) == []
    assert agent_updates == []


def test_voice_command_feedback_is_visible_in_the_present_page(present):
    _window, page = present

    page.show_voice_command_status("Opening Google Search in Chrome…", timeout_ms=0)

    assert page.voice_command_status.isVisible()
    assert page.voice_command_status.text() == "Opening Google Search in Chrome…"

    page.show_voice_command_status("", timeout_ms=0)
    assert not page.voice_command_status.isVisible()


def test_corrected_speech_revokes_dispatched_native_action(present):
    _window, page = present
    page.transcript_context.bind("session", 1, str(page.speech_language.currentData()))
    sleep(0.11)  # Let the test utterance begin after the context capture boundary.

    def emit(sequence, segment_id, text, *, final=False):
        page.transcript_context.accept(
            {
                "session_id": "session",
                "stream_id": "stream",
                "sequence": sequence,
                "segment_id": segment_id,
                "text": text,
                "final": final,
                "start_seconds": 0.0,
                "end_seconds": 0.1,
                "capture_end_ns": monotonic_ns(),
            }
        )

    phrase = "Let's look at the table"
    emit(1, "active", phrase)
    emit(2, "active", phrase + " of contents")
    page._native_policy_context = page.transcript_context.snapshot()
    page._native_guard.set()
    delayed_office_guard = page._native_guard.is_set

    emit(3, "active", phrase + " of contents while I keep explaining")
    page._revoke_native_guard_if_speech_changed()
    assert page._native_guard.is_set()  # Normal continuation keeps the dispatched task live.

    emit(4, "correction", "Actually, do not go to the next slide.", final=True)
    page._revoke_native_guard_if_speech_changed()
    assert not page._native_guard.is_set()
    assert not delayed_office_guard()  # A held Office task cannot commit after the correction.
    assert page._native_policy_context is None


def test_open_dialog_and_all_six_buttons(desktop_window, qtbot, monkeypatch):
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    assert not page.next_button.isEnabled() and not page.highlight_button.isEnabled()
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *args: (str(FIXTURES / "manifest.json"), "")
    )
    qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    qtbot.mouseClick(page.next_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 2 of 3"
    qtbot.mouseClick(page.previous_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 1 of 3"
    page.target.setCurrentIndex(2)
    qtbot.mouseClick(page.go_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.counter.text() == "Slide 3 of 3"
    region = {"x": 80, "y": 64, "width": 800, "height": 200}
    region_fields(page, region)
    qtbot.mouseClick(page.zoom_button, Qt.MouseButton.LeftButton)
    assert page.surface.region == Region(**region)
    region_fields(page, {"x": 100, "y": 100, "width": 100, "height": 20})
    qtbot.mouseClick(page.highlight_button, Qt.MouseButton.LeftButton)
    assert page.surface.highlight == Region(100, 100, 100, 20)
    assert page.surface.region == Region(**region)
    qtbot.mouseClick(page.reset_button, Qt.MouseButton.LeftButton)
    assert page.surface.highlight is None
    assert page.surface.region == page.surface.slide.full
    assert page.surface.selection is None
    assert not page.highlight_button.isEnabled()


def test_drag_reversed_drag_letterbox_and_zoomed_selection(present, qtbot):
    _, page = present
    surface = page.surface
    region_fields(page, {"x": 200, "y": 100, "width": 1200, "height": 600})
    page.zoom_button.click()
    mapping = surface.mapping()
    start = mapping.point(300, 200).toPoint()
    end = mapping.point(500, 300).toPoint()
    qtbot.mousePress(surface, Qt.MouseButton.LeftButton, pos=end)
    qtbot.mouseMove(surface, start)
    qtbot.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=start)
    page.highlight_button.click()
    selected = surface.highlight
    assert abs(selected.x - 300) < 6 and abs(selected.y - 200) < 6
    assert abs(selected.width - 200) < 6 and abs(selected.height - 100) < 6
    page.reset_button.click()
    surface.resize(400, 400)
    # Top corner is outside the wide slide's letterboxed image, so cannot start a selection.
    qtbot.mousePress(surface, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
    qtbot.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert surface.selection is None


def test_empty_click_invalid_fields_boundary_and_failed_open(present, qtbot, tmp_path):
    _, page = present
    qtbot.mouseClick(page.surface, Qt.MouseButton.LeftButton, pos=page.surface.rect().center())
    assert not page.highlight_button.isEnabled()
    region_fields(page, {"x": 1910, "y": 100, "width": 100, "height": 20})
    page.highlight_button.click()
    assert "bounds" in page.feedback.text()
    assert page.surface.highlight is None
    region_fields(page, {"x": 80, "y": 64, "width": 100, "height": 20})
    page.highlight_button.click()
    before = page.surface.highlight
    page.previous_button.click()
    assert page.surface.highlight == before
    page.next_button.click()
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.surface.highlight is None
    assert page.surface.selection is None
    page.load_manifest(tmp_path / "missing.json")
    qtbot.waitUntil(lambda: not page.executor.busy)
    assert page.reset_button.isEnabled()
    assert page.counter.text() == "Slide 2 of 3"
    assert "JSON" in page.feedback.text()


def test_close_during_slide_read_waits_without_blocking(present, qtbot, monkeypatch):
    import buddy_desktop.slide_executor as module

    window, page = present
    entered, release = Event(), Event()
    real_load = module.load_image

    def delayed(manifest, slide):
        entered.set()
        assert release.wait(4)
        return real_load(manifest, slide)

    monkeypatch.setattr(module, "load_image", delayed)
    page.next_button.click()
    qtbot.waitUntil(entered.is_set)
    assert not page.next_button.isEnabled()
    assert page.reset_button.isEnabled()
    try:
        assert not window.close()
        assert window.isVisible()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.executor.busy)
    qtbot.waitUntil(lambda: not window.isVisible())
    assert page.executor.current_index == 0


def test_return_by_voice_focuses_the_powerpoint_slideshow(present, monkeypatch):
    # #236: PowerPoint presents in its own slideshow window, not in Buddy.
    import buddy_desktop.window as window_module

    window, page = present
    focused = []
    monkeypatch.setattr(
        window_module, "activate_native_window", lambda h: focused.append(h) or True
    )
    monkeypatch.setattr(
        window_module, "activate_qt_window", lambda w: pytest.fail("Buddy window was focused")
    )
    monkeypatch.setattr(page, "powerpoint_slideshow_window", lambda: 4242)
    window._return_to_presentation_by_voice()
    assert focused == [4242]


def test_powerpoint_slideshow_window_is_none_without_a_powerpoint_session(present):
    _window, page = present
    assert page.powerpoint_slideshow_window() is None


def test_search_panel_covers_the_powerpoint_slideshow_screen(present, monkeypatch):
    # #250: when PowerPoint presents, the answer panel goes over its slideshow screen.
    from PySide6.QtGui import QGuiApplication

    import buddy_desktop.window as window_module

    window, page = present
    screen = QGuiApplication.primaryScreen()
    area, ratio = screen.geometry(), screen.devicePixelRatio()
    physical = (
        area.x(),
        area.y(),
        area.x() + round(area.width() * ratio),
        area.y() + round(area.height() * ratio),
    )
    monkeypatch.setattr(page, "powerpoint_slideshow_window", lambda: 4242)
    monkeypatch.setattr(window_module, "native_window_rect", lambda h: physical)
    assert window._powerpoint_slideshow_area() == area

    shown = []
    monkeypatch.setattr(
        window._information_search_panel(),
        "present",
        lambda owner, query, images=False: shown.append(owner),
    )
    window._open_voice_search("definition of churn", False)
    assert shown == [area]


def test_text_search_starts_at_once_and_a_correction_replaces_it(present, qtbot):
    # #250: the in-presentation panel does not wait 3 s; "I mean" sends a new query.
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    spy = QSignalSpy(page.web_search_requested)

    def say(text, segment):
        page._speech_transcript(
            {
                "text": text,
                "final": True,
                "segment_id": segment,
                "sequence": len(segment),
                "start_seconds": 0.0,
                "end_seconds": 0.7,
                "latency_ms": 20,
            }
        )

    say("Search for electric cars", "s1")
    qtbot.waitUntil(lambda: spy.count() == 1, timeout=3000)
    assert spy.at(0) == ["electric cars", False, False]
    say("I mean hybrid cars", "s12")
    qtbot.waitUntil(lambda: spy.count() == 2, timeout=3000)
    assert spy.at(1) == ["hybrid cars", False, False]


def test_image_search_opens_the_panel_at_once_without_chrome(present, qtbot):
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    spy = QSignalSpy(page.web_search_requested)
    page._speech_transcript(
        {
            "text": "Show me images of red pandas",
            "final": True,
            "segment_id": "images-panel",
            "sequence": 1,
            "start_seconds": 0.0,
            "end_seconds": 0.7,
            "latency_ms": 20,
        }
    )
    qtbot.waitUntil(lambda: spy.count() == 1, timeout=3000)
    assert spy.at(0) == ["red pandas", True, False]


def test_window_recreates_a_deleted_search_panel(present):
    # #250: even if the panel is ever deleted, the next search gets a working panel.
    import shiboken6

    window, _page = present
    first = window._information_search_panel()
    shiboken6.delete(first)
    second = window._information_search_panel()
    assert second is not first and shiboken6.isValid(second)
    window._open_voice_search("red pandas", True)
    assert second.isVisible()
    window._close_information_search()


def test_search_panel_shows_on_screen_when_the_workspace_is_hidden(present, monkeypatch):
    # #250: in Buddy Mini the main window is hidden; the panel goes on the screen instead.
    window, _page = present
    shown = []
    panel = window._information_search_panel()
    monkeypatch.setattr(panel, "present", lambda owner, query, images=False: shown.append(owner))
    monkeypatch.setattr(window, "isVisible", lambda: False)
    window._open_voice_search("red pandas", True)
    assert shown and shown[0] == window.screen().availableGeometry()


def _jev_search(page, segments, kind="web"):
    from types import SimpleNamespace

    context = SimpleNamespace(
        decision_segments=tuple(
            SimpleNamespace(segment_id=sid, text=text, final=final) for sid, text, final in segments
        )
    )
    page._spoken_search_decided({"search": kind, "_client_context": context})


def test_jev_search_decision_searches_the_finished_sentence_once(present):
    # Jev decided "search"; the panel gets the sentence and the one before it.
    from PySide6.QtTest import QSignalSpy

    _window, page = present
    spy = QSignalSpy(page.spoken_search_requested)
    segments = [
        ("a", "Our Dubai office opened in March.", True),
        ("b", "I'm curious how tall the Burj Khalifa is, let's check.", True),
    ]
    _jev_search(page, segments)
    _jev_search(page, segments)  # The same sentence never searches twice.
    assert spy.count() == 1
    assert spy.at(0) == [
        "I'm curious how tall the Burj Khalifa is, let's check.",
        "Our Dubai office opened in March.",
        False,
    ]


def test_jev_search_on_unfinished_speech_waits_for_the_final_words(present, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtTest import QSignalSpy

    _window, page = present
    spy = QSignalSpy(page.spoken_search_requested)
    _jev_search(page, [("c", "Let's see some pictures of", False)], kind="images")
    assert spy.count() == 0
    final = SimpleNamespace(
        segment_id="c", text="Let's see some pictures of red pandas.", final=True
    )
    monkeypatch.setattr(
        page.transcript_context, "snapshot", lambda: SimpleNamespace(segments=(final,))
    )
    page._finish_pending_spoken_search()
    monkeypatch.undo()
    assert spy.count() == 1
    assert spy.at(0) == ["Let's see some pictures of red pandas.", "", True]
    page._finish_pending_spoken_search()
    assert spy.count() == 1


def test_spoken_search_reaches_the_service_as_speech(present, monkeypatch):
    window, _page = present
    calls = []

    class Service:
        def __init__(self, _config):
            pass

        def answer(self, access, query, *, images=False, spoken_context=None):
            calls.append((query, images, spoken_context))
            raise RuntimeError("stop here")

    monkeypatch.setattr("buddy_desktop.window.InformationSearchService", Service)
    monkeypatch.setattr(window.account, "session", object(), raising=False)
    monkeypatch.setattr(window.account, "account_id", "account", raising=False)
    monkeypatch.setattr(window, "_access_provider", lambda _session: lambda: "token")
    window._open_spoken_search("Let's find out who founded Tesla.", "Next topic.", False)
    import time

    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        time.sleep(0.01)
    assert calls == [("Let's find out who founded Tesla.", False, "Next topic.")]
    window._close_information_search()


def test_jev_panel_decisions_close_or_scroll_once_per_sentence(present):
    from types import SimpleNamespace

    from PySide6.QtTest import QSignalSpy

    _window, page = present
    closes = QSignalSpy(page.return_to_presentation_requested)
    scrolls = QSignalSpy(page.scroll_panel_requested)

    def decide(segment, panel):
        context = SimpleNamespace(
            decision_segments=(SimpleNamespace(segment_id=segment, text="x", final=True),)
        )
        page._panel_action_decided({"panel": panel, "_client_context": context})

    decide("p1", "scroll_down")
    decide("p1", "scroll_down")  # The same sentence acts once.
    decide("p2", "scroll_up")
    decide("p3", "close")
    assert [scrolls.at(i) for i in range(scrolls.count())] == [[1], [-1]]
    assert closes.count() == 1


def test_window_tells_the_agent_while_the_search_panel_is_on_screen(present):
    window, page = present
    assert page.agent_service.search_panel_open is False
    window._open_voice_search("red pandas", True)
    assert page.agent_service.search_panel_open is True
    window._information_search_panel().hide()
    assert page.agent_service.search_panel_open is False


def test_current_slide_image_is_uploaded_once_per_slide(qapp):
    # #259: Jev sees the slide on screen; one upload per slide change, never a repeat.
    from types import SimpleNamespace

    from PySide6.QtGui import QColor, QImage

    from buddy_desktop.config import AppConfig
    from buddy_desktop.present_widget import PresentWidget

    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    slides = [SimpleNamespace(slide_id="s1"), SimpleNamespace(slide_id="s2")]
    jobs = []
    page = SimpleNamespace(
        config=AppConfig("http://127.0.0.1:9"),
        executor=SimpleNamespace(
            manifest=SimpleNamespace(slides=slides),
            _cache_key=lambda manifest, slide: slide.slide_id,
            _cached_image=lambda key: image,
        ),
        slide_image_runner=SimpleNamespace(busy=False, submit=jobs.append),
        _session_access=lambda: "token",
        _powerpoint_info=None,
        _powerpoint_frame=None,
        _slide_image_sent=None,
    )
    session = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
    for slide_id in ("s1", "s1", "s2"):
        PresentWidget._sync_slide_image(page, session, slide_id)
    assert len(jobs) == 2
    # PowerPoint waits for its own preview of the current slide.
    page._powerpoint_info, page._powerpoint_frame = SimpleNamespace(), ("s2", b"unused")
    PresentWidget._sync_slide_image(page, session, "s3")
    assert len(jobs) == 2
