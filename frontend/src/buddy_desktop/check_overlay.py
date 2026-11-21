"""Full-window notice shown before and while Buddy checks PowerPoint actions on this PC."""

import math
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
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
COUNTDOWN_SECONDS = 10
DO_NOT_TOUCH = (
    "Please don't use the mouse or keyboard until this finishes. PowerPoint will "
    "open full screen several times while Buddy tries each action and takes "
    "screenshots; touching anything can spoil a check."
)


class CheckOverlay(QWidget):
    """Covers Buddy's window, warns the user not to touch anything and offers Stop.

    PowerPoint opens full screen for each check and Buddy takes screenshots of the
    real slideshow. Mouse or keyboard input can move the pointer into the slide,
    switch windows or end the show, which would spoil the evidence. Automatic checks
    first show a short countdown so the user knows what is about to happen.
    """

    stop_requested = Signal()
    start_requested = Signal()
    later_requested = Signal()

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
        self.message = QLabel(DO_NOT_TOUCH)
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
        # Countdown buttons, shown only before an automatic run starts.
        self.later = QPushButton("Not now")
        self.later.setAccessibleName("Do not check PowerPoint actions now")
        self.later.clicked.connect(self._later)
        self.start_now = QPushButton("Start now")
        self.start_now.setAccessibleName("Start checking PowerPoint actions now")
        self.start_now.clicked.connect(self._start_now)
        for widget in (self.title, self.message, self.bar, self.step, self.hint):
            layout.addWidget(widget)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (self.later, self.start_now, self.stop):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        # Count down against the real clock, not timer ticks: a busy window can
        # deliver ticks late, which would stretch every "second".
        self.countdown = QTimer(self)
        self.countdown.setInterval(200)
        self.countdown.timeout.connect(self._tick)
        self.deadline = 0.0
        self.remaining = 0
        self.pending_total = 0
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

    def _cover(self, host: QWidget | None) -> None:
        current = self.parentWidget()
        if host is not None and host is not current:
            if current is not None:
                current.removeEventFilter(self)
            self.setParent(host)
            host.installEventFilter(self)
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self.setFocus()

    def announce(self, total: int, host: QWidget | None = None) -> None:
        """Tell the user an automatic check is about to start; it starts on its own."""
        self.pending_total = total
        self.deadline = time.monotonic() + COUNTDOWN_SECONDS
        self.remaining = COUNTDOWN_SECONDS
        self.title.setText("Buddy will check your PowerPoint actions")
        self.message.setText(
            f"{total} action{'s' if total != 1 else ''} need a quick check on this PC before "
            "they can be used live. " + DO_NOT_TOUCH
        )
        self.bar.hide()
        self.hint.setText("Choose Not now to check later from Review.")
        self.stop.hide()
        self.later.show()
        self.start_now.show()
        self._show_countdown()
        self._cover(host)
        self.countdown.start()

    def start(self, total: int, host: QWidget | None = None) -> None:
        """Cover ``host`` (normally Buddy's whole window) for the duration of a run."""
        self.countdown.stop()
        self.title.setText("Checking your PowerPoint actions")
        self.message.setText(DO_NOT_TOUCH)
        self.hint.setText(
            "Keep Buddy open. Each action takes about 10–30 seconds. Results become "
            "Ready automatically."
        )
        self.bar.show()
        self.later.hide()
        self.start_now.hide()
        self.stop.show()
        self.stop.setEnabled(True)
        self.stop.setText("Stop after this action")
        self.update_progress(0, total, 0)
        self._cover(host)

    def hide(self) -> None:
        self.countdown.stop()
        super().hide()

    def update_progress(self, done: int, total: int, attempt: int) -> None:
        self.bar.set_progress(done, total)
        current = min(done + 1, total)
        self.step.setText(
            f"Checking action {current} of {total}"
            + (f" · attempt {attempt} of 3" if attempt > 1 else "")
            + f" · {done} done"
        )

    def _show_countdown(self) -> None:
        self.step.setText(f"Starting in {self.remaining} seconds…")

    def _tick(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            self._start_now()
            return
        remaining = math.ceil(left)
        if remaining != self.remaining:
            self.remaining = remaining
            self._show_countdown()

    def _start_now(self) -> None:
        self.countdown.stop()
        self.start_requested.emit()

    def _later(self) -> None:
        self.countdown.stop()
        super().hide()
        self.later_requested.emit()

    def _stop(self) -> None:
        self.stop.setEnabled(False)
        self.stop.setText("Stopping after this action…")
        self.stop_requested.emit()

    def keyPressEvent(self, event: object) -> None:
        # Swallow keys so nothing underneath reacts during a check.
        return None
