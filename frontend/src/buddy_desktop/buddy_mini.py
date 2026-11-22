"""A separate desktop surface for the current presentation session."""

import math
from ctypes import wintypes
from importlib.resources import files

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QEnterEvent,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import QMenu, QPushButton, QWidget

from buddy_desktop.visuals import icon

CAPTION_SILENCE_SECONDS = 3.0


def mini_caption(state: str, latest: tuple[str, float] | None, now: float) -> str | None:
    """Caption for Mini: recent speech while listening, "" for silence, None when not listening."""
    if state != "Listening":
        return None
    if latest is None or not 0 <= now - latest[1] <= CAPTION_SILENCE_SECONDS:
        return ""
    return latest[0]


class BuddyMini(QWidget):
    _mascot_radius = 54
    _control_radius = 82
    # One-line live caption under the mascot: kept close to the mascot's width.
    _caption_gap = 6
    _caption_height = 24
    _caption_max_width = 136
    _caption_lift = _caption_gap + _caption_height

    restore_requested = Signal()
    settings_requested = Signal()
    closed = Signal()
    chime_finished = Signal()
    automation_requested = Signal()
    system_sleep = Signal()

    def __init__(self, mic: QPushButton, undo: QPushButton) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setWindowTitle("Buddy Mini")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFixedSize(206, 174 + self._caption_lift)
        self._top = False
        self._caption: str | None = None
        self._asset = QPixmap()
        self._asset.loadFromData(files("buddy_desktop").joinpath("assets/buddy.png").read_bytes())
        tint = QPainter(self._asset)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        tint.fillRect(self._asset.rect(), QColor(255, 225, 175, 18))
        tint.end()
        self._center = QPoint(self.width() // 2, 0)
        self._phase = 0
        self._reaction = 0
        self._result = ""
        self._listening = False
        self.reduced_motion = False
        self._hover_close = QTimer(self, singleShot=True, interval=320)
        self._hover_close.timeout.connect(self._close_if_outside)
        self._open = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._chime: QSoundEffect | None = None
        self._chime_tail = QTimer(self, singleShot=True, interval=300)
        self._chime_tail.timeout.connect(self.chime_finished)
        self.mascot = QPushButton(self)
        self.mascot.setFixedSize(2 * self._mascot_radius, 2 * self._mascot_radius)
        self.mascot.setAccessibleName("Buddy Mini controls")
        self.mascot.setToolTip("Buddy Mini controls. Escape opens Buddy.")
        self.mascot.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"border-radius: {self._mascot_radius}px; }}"
            "QPushButton:focus { border: 2px solid #ff9f61; }"
        )
        self.mascot.setMouseTracking(True)
        self.mascot.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mascot.customContextMenuRequested.connect(self._show_close_menu)
        controls = (
            (
                "mic",
                "Microphone",
                lambda: (
                    mic.click() if not self.chiming or mic.text() == "Stop microphone" else None
                ),
            ),
            ("play", "Live suggestions and automatic moves", self.automation_requested.emit),
            ("rotate-ccw", "Undo automatic move", undo.click),
            ("settings-2", "Settings", self.settings_requested.emit),
            ("external-link", "Open Buddy", self.restore_requested.emit),
        )
        self.buttons = []
        for glyph, name, callback in controls:
            button = QPushButton(self)
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setIcon(icon(glyph, "#000000"))
            button.setIconSize(QSize(17, 17))
            button.setFixedSize(32, 32)
            button.setStyleSheet(
                "QPushButton { background: #f5843d; border: 1px solid #e56c27; "
                "border-radius: 16px; padding: 0; min-width: 0; min-height: 0; }"
                "QPushButton:hover, QPushButton:focus { border: 2px solid #171717; }"
                "QPushButton:disabled { background: #b88060; border-color: #b88060; }"
            )
            button.clicked.connect(callback)
            self.buttons.append(button)
        self.set_top_corner(False)
        self._set_controls(False)
        self.mascot.clicked.connect(lambda: self._set_controls(True))

    def _show_close_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Close Mini and stop listening", self.close)
        menu.exec(self.mascot.mapToGlobal(pos))

    def _set_controls(self, visible: bool) -> None:
        self._open = visible
        for button in self.buttons:
            button.setVisible(visible)
        self.update()

    def sync_automation(self, available: bool, enabled: bool) -> None:
        button = self.buttons[1]
        label = f"{'Turn off' if enabled else 'Turn on'} live suggestions and automatic moves"
        button.setEnabled(available or enabled)
        if button.property("automationOn") != enabled:
            button.setProperty("automationOn", enabled)
            button.setIcon(icon("pause" if enabled else "play", "#000000"))
        button.setAccessibleName(label)
        button.setToolTip(label)

    def set_top_corner(self, top: bool) -> None:
        self._top = top
        margin = self._mascot_radius + 7
        # In a bottom corner Buddy lifts only while a caption needs the room below it.
        lift = self._caption_lift if self._caption is not None and not top else 0
        self._center.setY(margin if top else self.height() - margin - lift)
        self.mascot.move(self._center - QPoint(self._mascot_radius, self._mascot_radius))
        for index, button in enumerate(self.buttons):
            angle = math.radians(208 + index * 31)
            x = self._center.x() + self._control_radius * math.cos(angle)
            offset_y = self._control_radius * math.sin(angle)
            y = self._center.y() + (-offset_y if top else offset_y)
            button.move(round(x - button.width() / 2), round(y - button.height() / 2))

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover_close.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover_close.start()
        super().leaveEvent(event)

    def _close_if_outside(self) -> None:
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())) and not any(
            button.hasFocus() for button in (self.mascot, *self.buttons)
        ):
            self._set_controls(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.restore_requested.emit()
        else:
            super().keyPressEvent(event)

    def nativeEvent(
        self, event_type: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> tuple[bool, int]:
        if event_type == b"windows_generic_MSG":
            event = wintypes.MSG.from_address(int(message))
            # WM_POWERBROADCAST: suspend, automatic resume, or user-triggered resume.
            if event.message == 0x0218 and event.wParam in {0x0004, 0x0012, 0x0007}:
                self.system_sleep.emit()
                return True, 1
        return False, 0

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def sync(
        self, state: str, mic: QPushButton, undo: QPushButton, *, connected: bool = True
    ) -> None:
        self._listening = state == "Listening"
        self.mascot.setAccessibleName(f"Buddy Mini — {state}")
        self.mascot.setToolTip(f"{state}. Click for controls.")
        for mini, full in ((self.buttons[0], mic), (self.buttons[2], undo)):
            mini.setEnabled(connected and full.isEnabled())
            mini.setAccessibleName(full.text())
            mini.setToolTip(full.text())
        self.update()

    def set_caption(self, text: str | None) -> None:
        """Show one line of live speech, "" for listening silence, or None to hide it."""
        caption = None if text is None else " ".join(text.split())
        if caption == self._caption:
            return
        moved = (caption is None) != (self._caption is None)
        self._caption = caption
        self.mascot.setAccessibleDescription(caption or "")
        if moved:
            self.set_top_corner(self._top)
        self.update()

    def caption_rect(self) -> QRect | None:
        if self._caption is None or self._open:
            return None
        metrics = self.fontMetrics()
        text = self.caption_text()
        width = min(self._caption_max_width, max(44, metrics.horizontalAdvance(text) + 20))
        top = self._center.y() + self._mascot_radius + self._caption_gap
        return QRect(self._center.x() - width // 2, top, width, self._caption_height)

    def caption_text(self) -> str:
        """The newest words that fit on one line; older words drop off the left."""
        if not self._caption:
            return "• • •"
        metrics = self.fontMetrics()
        words = self._caption.split()
        while len(words) > 1 and (
            metrics.horizontalAdvance(" ".join(words)) > self._caption_max_width - 20
        ):
            words.pop(0)
        text = " ".join(words)
        # A single very long word keeps its end rather than overflowing the pill.
        while len(text) > 1 and metrics.horizontalAdvance(text) > self._caption_max_width - 20:
            text = text[1:]
        return text

    @property
    def chiming(self) -> bool:
        return self._chime_tail.isActive() or bool(self._chime and self._chime.isPlaying())

    def prepare_chime(self) -> bool:
        if self._chime is None:
            self._chime = QSoundEffect(self)
            self._chime.playingChanged.connect(self._chime_changed)
            path = files("buddy_desktop").joinpath("assets/mini-chime.wav")
            self._chime.setSource(QUrl.fromLocalFile(str(path)))
            self._chime.setVolume(0.22)
        return self._chime.status() == QSoundEffect.Status.Ready and not self.chiming

    def play_chime(self) -> None:
        if self._chime is not None:
            self._chime_tail.start()
            self._chime.play()

    def _chime_changed(self) -> None:
        if self._chime is not None:
            if self._chime.isPlaying():
                self._chime_tail.stop()
            else:
                self._chime_tail.start()

    def stop_sound(self) -> None:
        if self._chime is not None:
            self._chime.stop()
        self._chime_tail.stop()

    def completed(self, label: str = "") -> None:
        self._result = label
        self.mascot.setAccessibleDescription(label)
        self._reaction = 20
        self.update()

    def _tick(self) -> None:
        if not self.isVisible():
            return
        cursor = self.mapFromGlobal(QCursor.pos())
        if not self._open and self.mascot.geometry().contains(cursor):
            self._set_controls(True)
        elif self._open:
            if self.rect().contains(cursor):
                self._hover_close.stop()
            elif not self._hover_close.isActive():
                self._hover_close.start()
        if self._reaction or not self.reduced_motion:
            self._phase = (self._phase + 1) % 240
            self._reaction = max(0, self._reaction - 1)
            if not self._reaction and self._result:
                self._result = ""
                self.mascot.setAccessibleDescription("")
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        x, y = self._center.x(), self._center.y()
        if self._listening:
            for offset in (0, 12):
                phase = 12 if self.reduced_motion else (self._phase + offset) % 24
                radius = self._mascot_radius + 2 + phase // 2
                painter.setPen(QPen(QColor(255, 164, 80, 180 - phase * 5), 1.6))
                circle = QRectF(x - radius, y - radius, radius * 2, radius * 2)
                painter.drawArc(circle, -32 * 16, 64 * 16)
                painter.drawArc(circle, 148 * 16, 64 * 16)
        motion = 0.0 if self.reduced_motion else self._phase * math.tau / 120
        lift = -2 * math.sin(motion)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 6, 0, 30))
        painter.drawEllipse(QRectF(x - 38, y + self._mascot_radius - 7, 76, 9))
        painter.save()
        painter.translate(x, y + lift)
        if not self.reduced_motion:
            cursor = self.mapFromGlobal(QCursor.pos())
            lean = max(-6.0, min(6.0, (cursor.x() - x) / 10)) if self._open else 0.0
            painter.rotate(3 * math.sin(motion + 0.5) + lean)
            painter.scale(1 + 0.018 * math.sin(motion), 1 - 0.015 * math.sin(motion))
        radius = self._mascot_radius
        painter.drawPixmap(QRect(-radius, -radius, 2 * radius, 2 * radius), self._asset)
        if self._reaction:
            painter.setBrush(QColor(255, 215, 125, 220))
            for eye_x in (-13, 13):
                painter.drawEllipse(QPoint(eye_x, -3), 2, 2)
        painter.restore()
        caption = self.caption_rect()
        if caption is not None:
            painter.setPen(QPen(QColor("#e56c27"), 1))
            painter.setBrush(QColor("#f5843d"))
            painter.drawRoundedRect(QRectF(caption).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            painter.setPen(QColor("#000000"))
            painter.drawText(caption, Qt.AlignmentFlag.AlignCenter, self.caption_text())
        if self._reaction:
            painter.setPen(Qt.PenStyle.NoPen)
            if self._result and not self._open:
                badge_y = y - radius - 28 if y > self.height() / 2 else y + radius + 4
                badge = QRect(x - 55, badge_y, 110, 24)
                painter.setBrush(QColor("#171717"))
                painter.drawRoundedRect(badge, 12, 12)
                painter.setPen(QColor("#fff4e9"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._result)
