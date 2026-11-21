"""Full-window notice shown while Buddy checks PowerPoint actions on this PC."""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.views import ProgressStrip

CARD_WIDTH = 520


class CheckOverlay(QWidget):
    """Covers Buddy's window, asks the user not to touch anything and offers Stop.

    PowerPoint opens full screen for each check and Buddy takes screenshots of the
    real slideshow. Mouse or keyboard input can move the pointer into the slide,
    switch windows or end the show, which would spoil the evidence.
    """

    stop_requested = Signal()

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setObjectName("checkOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Block clicks and keys from reaching Buddy underneath while checks run.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Checking PowerPoint actions")
        card = self.card = QFrame(self)
        card.setObjectName("checkOverlayCard")
        # A fixed width lets wrapped text get its full height (no clipped lines).
        card.setFixedWidth(CARD_WIDTH)
        card.setStyleSheet(
            "#checkOverlayCard { background: #141418; border: 1px solid #2A2B31;"
            " border-radius: 16px; }"
            " #checkOverlayCard QLabel { background: transparent; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(12)
        self.title = QLabel("Checking your PowerPoint actions")
        self.title.setObjectName("sectionTitle")
        self.title.setWordWrap(True)
        self.message = QLabel(
            "Please don't use the mouse or keyboard until this finishes. PowerPoint will "
            "open full screen several times while Buddy tries each action and takes "
            "screenshots; touching anything can spoil a check."
        )
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #D9D9DE;")
        self.bar = ProgressStrip()
        self.step = QLabel()
        self.step.setWordWrap(True)
        self.step.setStyleSheet("color: #FFB477;")
        self.hint = QLabel(
            "Keep Buddy open. Each action takes about 10–30 seconds. Results become "
            "Ready automatically."
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #9A9AA3;")
        self.stop = QPushButton("Stop after this action")
        self.stop.setAccessibleName("Stop checking after the current action")
        self.stop.clicked.connect(self._stop)
        for widget in (self.title, self.message, self.bar, self.step, self.hint):
            layout.addWidget(widget)
        layout.addWidget(self.stop, 0, Qt.AlignmentFlag.AlignRight)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        host.installEventFilter(self)
        self.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parent() and event.type() == QEvent.Type.Resize:
            host = self.parentWidget()
            if host is not None:
                self.setGeometry(host.rect())
        return False

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 205))
        painter.end()

    def start(self, total: int, host: QWidget | None = None) -> None:
        """Cover ``host`` (normally Buddy's whole window) for the duration of a run."""
        current = self.parentWidget()
        if host is not None and host is not current:
            if current is not None:
                current.removeEventFilter(self)
            self.setParent(host)
            host.installEventFilter(self)
        self.stop.setEnabled(True)
        self.stop.setText("Stop after this action")
        self.update_progress(0, total, 0)
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self.setFocus()

    def update_progress(self, done: int, total: int, attempt: int) -> None:
        self.bar.set_progress(done, total)
        current = min(done + 1, total)
        self.step.setText(
            f"Checking action {current} of {total}"
            + (f" · attempt {attempt} of 3" if attempt > 1 else "")
            + f" · {done} done"
        )

    def _stop(self) -> None:
        self.stop.setEnabled(False)
        self.stop.setText("Stopping after this action…")
        self.stop_requested.emit()

    def keyPressEvent(self, event: object) -> None:
        # Swallow keys so nothing underneath reacts during a check.
        return None
