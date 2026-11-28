"""The guide remains usable offline and preserves existing workspace routes."""

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QPushButton


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_tutorial_navigation_scroll_and_actions(desktop_window, qapp, size):
    window = desktop_window()
    window.resize(*size)
    assert window.account.session is None
    window.navigation.setCurrentRow(5)
    qapp.processEvents()
    assert window.pages.currentWidget().widget() is window.tutorial
    assert window.route_title.text() == "How to use"
    scroll = window.pages.currentWidget()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() > 0
    text = "\n".join(label.text() for label in window.tutorial.findChildren(QLabel))
    assert "Open PDF" in text and "Execution validation" in text
    assert "PDF actions are checked automatically" in text
    assert "Check on this PC" in text
    assert "Deepgram in the EU" in text
    assert "does not automatically navigate slides" not in text
    assert "PowerPoint slideshow stays local and is not transcribed" not in text
    assert "saved PDF and saved PowerPoint sessions" in text
    for button in (
        window.review.open_powerpoint,
        window.review.present_powerpoint,
        window.review.open_evidence,
        window.present.policy_undo_button,
    ):
        assert button.text() in text
    assert window.present.agent_enabled.text() in text
    assert window.present.policy_enabled.text() in text
    assert "Ctrl+Shift+P" in text and "Ctrl+Shift+Z" in text
    assert "Reset zoom" in text and "PPTX" in text
    assert not window.runner.busy and not window.library.runner.busy
    for title, destination in (("Open Present", 3), ("Open Library", 1), ("Open Settings", 4)):
        window.navigation.setCurrentRow(5)
        button = next(b for b in window.tutorial.findChildren(QPushButton) if b.text() == title)
        button.click()
        assert window.pages.currentIndex() == destination
    tutorial_shortcut = next(a for a in window.findChildren(QAction) if a.text() == "&How to use")
    tutorial_shortcut.trigger()
    assert window.pages.currentIndex() == 6
    window._show_account()
    assert window.pages.currentIndex() == 5
    assert window.route_title.text() == "Profile"
