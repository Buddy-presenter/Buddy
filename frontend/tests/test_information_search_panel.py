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
    assert geometry.top() == area.top() + 16
    # Top half of the right-hand side.
    assert geometry.height() <= area.height() // 2


def test_panel_over_a_buddy_window_is_never_inside_it(qtbot):
    # #250: a panel inside a window is deleted with it; it must stay its own window.
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
    assert panel.parentWidget() is None
    assert not panel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_panel_survives_its_fullscreen_window_closing(qtbot):
    # #250: fullscreen presentations delete themselves when closed. The panel shown over
    # one must survive and show again after re-entering fullscreen.
    import shiboken6
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMainWindow

    panel = InformationSearchPanel(reduce_motion=True)
    qtbot.addWidget(panel)
    for attempt in range(3):
        fullscreen = QMainWindow()
        fullscreen.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        fullscreen.resize(1200, 800)
        fullscreen.show()
        panel.present(fullscreen, f"search {attempt}")
        assert panel.isVisible()
        fullscreen.close()
        qtbot.waitUntil(lambda w=fullscreen: not shiboken6.isValid(w), timeout=2000)
        assert shiboken6.isValid(panel)
        panel.close_panel()


def test_panel_scrolls_its_results(qtbot):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QLabel

    panel = InformationSearchPanel(reduce_motion=True)
    qtbot.addWidget(panel)
    panel.present(QRect(0, 0, 1600, 900), "long answer")
    for index in range(40):
        panel.content_layout.addWidget(QLabel(f"line {index}"))
    qtbot.waitUntil(lambda: panel._scroll.verticalScrollBar().maximum() > 0, timeout=1000)
    bar = panel._scroll.verticalScrollBar()
    panel.scroll_by(1)
    assert bar.value() > 0
    panel.scroll_by(-1)
    assert bar.value() == 0


def test_panel_window_is_translucent_outside_its_rounded_shape(qtbot):
    from PySide6.QtCore import Qt

    panel = InformationSearchPanel(reduce_motion=True)
    qtbot.addWidget(panel)
    assert panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
