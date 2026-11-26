from buddy_desktop.information_search_panel import InformationSearchPanel


def test_repeated_close_signal_keeps_exit_animation(qtbot):
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    owner.resize(1200, 800)
    qtbot.addWidget(owner)
    owner.show()

    panel = InformationSearchPanel()
    qtbot.addWidget(panel)
    panel.present(owner, "definition of opportunity cost")

    # The owning window also handles this signal to cancel stale requests.
    panel.close_requested.connect(panel.close_panel)
    panel.close_requested.emit()

    assert panel.isVisible()
    qtbot.waitUntil(lambda: not panel.isVisible(), timeout=1000)


def test_panel_over_a_powerpoint_slideshow_stays_on_top_without_taking_focus(qtbot):
    # #250: over PowerPoint's own slideshow window the panel floats on that screen.
    from PySide6.QtCore import QRect, Qt

    panel = InformationSearchPanel(reduce_motion=True)
    qtbot.addWidget(panel)
    area = QRect(100, 50, 1600, 900)
    panel.present(area, "definition of churn")

    flags = panel.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert panel.parentWidget() is None
    geometry = panel.frameGeometry()
    assert area.contains(geometry.topLeft()) and area.right() >= geometry.left()
    assert geometry.top() == area.top() + 24


def test_panel_returns_to_a_buddy_window_after_a_slideshow(qtbot):
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    owner.resize(1000, 700)
    qtbot.addWidget(owner)
    owner.show()
    panel = InformationSearchPanel(reduce_motion=True)
    qtbot.addWidget(panel)
    panel.present(QRect(0, 0, 800, 600), "first")
    panel.present(owner, "second")
    assert panel.parentWidget() is owner
    assert not panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
