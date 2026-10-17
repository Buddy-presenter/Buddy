"""Native presentation-only full screen using the existing live slide surface."""

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from buddy_desktop.slide_surface import SlideSurface


class FullscreenPresentation(QMainWindow):
    exited = Signal()

    def __init__(
        self,
        surface: SlideSurface,
        layout: QVBoxLayout,
        send: Callable[[str], None],
        zoom: Callable[[], None],
        highlight: Callable[[], None],
        go_to: Callable[[], None],
        owner: QWidget,
    ) -> None:
        super().__init__(owner.window())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Buddy presentation — Esc to exit")
        self.surface, self.original_layout = surface, layout
        self.original_index = layout.indexOf(surface)
        self.setGeometry(owner.window().geometry())
        self.setCentralWidget(surface)
        for shortcut, command in [
            ("Right", "next_slide"),
            ("Space", "next_slide"),
            ("PgDown", "next_slide"),
            ("Left", "previous_slide"),
            ("PgUp", "previous_slide"),
            ("R", "reset_view"),
        ]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, kind=command: send(kind))
            self.addAction(action)
        for shortcut in ("Esc", "F11"):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(self.close)
            self.addAction(action)
        for shortcut, callback in (("Z", zoom), ("H", highlight), ("G", go_to)):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)

    def closeEvent(self, event: QCloseEvent) -> None:
        surface = self.takeCentralWidget()
        if surface is not None:
            self.original_layout.insertWidget(self.original_index, surface, 1)
            surface.show()
        self.exited.emit()
        event.accept()
