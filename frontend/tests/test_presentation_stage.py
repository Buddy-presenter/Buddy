"""Presentation stage integration with saved-session work and display handling."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from buddy_desktop.config import AppConfig
from buddy_desktop.fullscreen import FullscreenPresentation
from buddy_desktop.present_widget import PresentWidget
from buddy_desktop.presentation_session_service import (
    PresentationSession,
    PresentationSessionService,
    SessionState,
)
from buddy_desktop.slide_contract import Manifest, Slide
from buddy_desktop.slide_surface import SlideSurface


def _manifest(deck_id: str = "local-deck") -> Manifest:
    root = Path(__file__).parent / "fixtures" / "slide_actions"
    asset = root / "wide.png"
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    slides = tuple(
        Slide(f"slide-{index}", 1920, 1080, "wide.png", 960, 540, digest) for index in (1, 2)
    )
    return Manifest(deck_id, "local-render", slides, root)


def _session(deck_id: str, state: SessionState, version: int = 1) -> PresentationSession:
    return PresentationSession(
        id=f"session-{deck_id}",
        owner_id="owner",
        deck_id=deck_id,
        analysis_version_id=f"analysis-{deck_id}",
        status="active",
        state_version=version,
        state=state,
        created_at=datetime.now(UTC),
    )


def test_manual_navigation_remains_responsive_while_session_request_is_pending(
    qtbot: QtBot, tmp_path: Path
) -> None:
    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    widget = PresentWidget(AppConfig("http://127.0.0.1:8000"), settings)
    qtbot.addWidget(widget)
    manifest = _manifest()
    widget.executor.manifest = manifest
    widget.executor.current_index = 0
    widget.surface.commit(
        manifest.slides[0], QImage(str(manifest.root / "wide.png")), manifest.slides[0].full
    )
    widget._refresh()
    widget._session_attempt = ("owner", manifest.deck_id)
    release = Event()
    widget.session_runner.submit(lambda: release.wait(5))
    assert widget.session_runner.busy

    widget.send("next_slide")
    qtbot.waitUntil(lambda: widget.executor.current_index == 1)
    assert widget.session_runner.busy
    assert widget.counter.text() == "Slide 2 of 2"
    assert widget._session_pending_state is not None
    assert widget._session_pending_state.active_slide_id == manifest.slides[1].slide_id

    release.set()
    qtbot.waitUntil(lambda: not widget.session_runner.busy)
    widget.shutdown()


def test_position_returned_to_acknowledged_state_is_saved_after_inflight_update(
    qtbot: QtBot, tmp_path: Path
) -> None:
    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    widget = PresentWidget(AppConfig("http://127.0.0.1:8000"), settings)
    qtbot.addWidget(widget)
    manifest = _manifest()
    first_state = SessionState(manifest.slides[0].slide_id)
    second_state = SessionState(manifest.slides[1].slide_id)
    widget.executor.manifest = manifest
    widget.executor.current_index = 0
    widget.surface.commit(
        manifest.slides[0], QImage(str(manifest.root / "wide.png")), manifest.slides[0].full
    )
    widget._account_id = "owner"
    widget._session_access = "test-access"
    widget._session_deck_id = manifest.deck_id
    widget._session_attempt = ("owner", manifest.deck_id)
    widget._session = _session(manifest.deck_id, first_state)
    update_started = Event()
    release_first_update = Event()
    sent_states: list[SessionState] = []

    class DelayedService:
        def update(
            self, _access: str, session: PresentationSession, state: SessionState
        ) -> PresentationSession:
            sent_states.append(state)
            if len(sent_states) == 1:
                update_started.set()
                release_first_update.wait(5)
            return _session(session.deck_id, state, session.state_version + 1)

    widget.session_service = cast(PresentationSessionService, DelayedService())
    try:
        widget.executor.current_index = 1
        widget.surface.commit(
            manifest.slides[1], QImage(str(manifest.root / "wide.png")), manifest.slides[1].full
        )
        widget._queue_session_state()
        qtbot.waitUntil(update_started.is_set)

        widget.executor.current_index = 0
        widget.surface.commit(
            manifest.slides[0], QImage(str(manifest.root / "wide.png")), manifest.slides[0].full
        )
        widget._queue_session_state()
        assert widget._session_pending_state == first_state

        release_first_update.set()
        qtbot.waitUntil(
            lambda: (
                not widget.session_runner.busy
                and len(sent_states) == 2
                and widget._session is not None
                and widget._session.state == first_state
            )
        )
        assert sent_states == [second_state, first_state]
        assert widget.surface.slide == manifest.slides[0]
    finally:
        release_first_update.set()
        widget.shutdown()


def test_new_deck_session_start_waits_for_previous_deck_request(
    qtbot: QtBot, tmp_path: Path
) -> None:
    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    widget = PresentWidget(AppConfig("http://127.0.0.1:8000"), settings)
    qtbot.addWidget(widget)
    first_manifest = replace(_manifest("first-deck"), account_bound=True)
    next_manifest = replace(_manifest("next-deck"), account_bound=True)
    first_started = Event()
    release_first = Event()
    started_decks: list[str] = []

    class DelayedService:
        def start(self, _access: str, deck_id: str) -> PresentationSession:
            started_decks.append(deck_id)
            if deck_id == first_manifest.deck_id:
                first_started.set()
                release_first.wait(5)
            state = SessionState(
                first_manifest.slides[0].slide_id
                if deck_id == first_manifest.deck_id
                else next_manifest.slides[0].slide_id
            )
            return _session(deck_id, state)

    widget.session_service = cast(PresentationSessionService, DelayedService())
    widget._account_id = "owner"
    widget._session_access = "test-access"
    widget.executor.manifest = first_manifest
    widget.executor.current_index = 0
    widget.surface.commit(
        first_manifest.slides[0],
        QImage(str(first_manifest.root / "wide.png")),
        first_manifest.slides[0].full,
    )
    try:
        widget._ensure_session()
        qtbot.waitUntil(first_started.is_set)

        widget.executor.manifest = next_manifest
        widget.executor.current_index = 0
        widget.surface.commit(
            next_manifest.slides[0],
            QImage(str(next_manifest.root / "wide.png")),
            next_manifest.slides[0].full,
        )
        widget._ensure_session()
        assert widget._session_followup == "ensure"
        assert widget._session_attempt is None

        release_first.set()
        qtbot.waitUntil(
            lambda: (
                not widget.session_runner.busy
                and widget._session is not None
                and widget._session.deck_id == next_manifest.deck_id
            )
        )
        assert started_decks == [first_manifest.deck_id, next_manifest.deck_id]
        assert widget._session_attempt == ("owner", next_manifest.deck_id)
    finally:
        release_first.set()
        widget.shutdown()


def test_fullscreen_moves_audience_surface_without_changing_slide_state(qtbot: QtBot) -> None:
    owner = QWidget()
    owner_layout = QVBoxLayout(owner)
    surface = SlideSurface()
    owner_layout.addWidget(surface)
    slide = Slide("slide-1", 800, 600, "", 800, 600, "0" * 64)
    surface.commit(slide, QImage(800, 600, QImage.Format.Format_ARGB32), slide.full)
    fullscreen = FullscreenPresentation(
        surface,
        owner_layout,
        lambda _kind: None,
        lambda: None,
        lambda: None,
        lambda: None,
        owner,
        QGuiApplication.primaryScreen(),
    )
    qtbot.addWidget(owner)
    qtbot.addWidget(fullscreen)
    fullscreen.move_to_screen(QGuiApplication.primaryScreen())
    fullscreen.showFullScreen()
    qtbot.waitUntil(fullscreen.isVisible)

    assert fullscreen.windowHandle().screen() == QGuiApplication.primaryScreen()
    assert surface.slide == slide
    assert surface.region == slide.full
    assert surface.mapping() is not None

    fullscreen.close()
    qtbot.waitUntil(lambda: owner_layout.indexOf(surface) >= 0)
