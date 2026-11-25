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
