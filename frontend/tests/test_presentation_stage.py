"""Presentation stage integration with saved-session work and display handling."""

import hashlib
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget

from buddy_desktop.config import AppConfig
from buddy_desktop.fullscreen import FullscreenPresentation
from buddy_desktop.present_widget import PresentWidget
from buddy_desktop.slide_contract import Manifest, Slide
from buddy_desktop.slide_surface import SlideSurface


def _manifest() -> Manifest:
    root = Path(__file__).parent / "fixtures" / "slide_actions"
    asset = root / "wide.png"
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    slides = tuple(
        Slide(f"slide-{index}", 1920, 1080, "wide.png", 960, 540, digest) for index in (1, 2)
    )
    return Manifest("local-deck", "local-render", slides, root)


def test_manual_navigation_remains_responsive_while_session_request_is_pending(qtbot, tmp_path):
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
    assert widget._session_pending_state.active_slide_id == manifest.slides[1].slide_id

    release.set()
    qtbot.waitUntil(lambda: not widget.session_runner.busy)
    widget.shutdown()


def test_fullscreen_moves_audience_surface_without_changing_slide_state(qtbot):
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
