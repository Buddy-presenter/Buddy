"""A separate desktop surface for the current presentation session."""

import math
from importlib.resources import files

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import QMenu, QPushButton, QWidget

from buddy_desktop.visuals import icon


class BuddyMini(QWidget):
    restore_requested = Signal()
    settings_requested = Signal()
    closed = Signal()
    chime_finished = Signal()
    automation_requested = Signal()

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
        self.setFixedSize(180, 158)
        self._asset = QPixmap()
        self._asset.loadFromData(files("buddy_desktop").joinpath("assets/buddy.png").read_bytes())
        self._center = QPoint(90, 111)
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
        self.mascot.setGeometry(48, 69, 84, 84)
        self.mascot.setAccessibleName("Buddy Mini controls")
        self.mascot.setToolTip("Buddy Mini controls. Escape opens Buddy.")
        self.mascot.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 42px; }"
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
            button.setIconSize(QSize(16, 16))
            button.setFixedSize(28, 28)
            button.setStyleSheet(
                "QPushButton { background: #f5843d; border: 1px solid #e56c27; "
                "border-radius: 14px; }"
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
        self._center.setY(47 if top else 111)
        self.mascot.move(48, self._center.y() - 42)
        for index, button in enumerate(self.buttons):
            angle = math.radians(194 + index * 38)
            x = round(76 + 68 * math.cos(angle))
            y = round(97 + 68 * math.sin(angle))
            button.move(x, self.height() - y - button.height() if top else y)

    def enterEvent(self, event) -> None:
        self._hover_close.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        y = self._center.y()
        if self._listening:
            for offset in (0, 12):
                phase = 12 if self.reduced_motion else (self._phase + offset) % 24
                radius = 44 + phase // 2
                painter.setPen(QPen(QColor(255, 164, 80, 180 - phase * 5), 1.6))
                circle = QRectF(90 - radius, y - radius, radius * 2, radius * 2)
                painter.drawArc(circle, -32 * 16, 64 * 16)
                painter.drawArc(circle, 148 * 16, 64 * 16)
        motion = 0.0 if self.reduced_motion else self._phase * math.tau / 120
        lift = -2 * math.sin(motion)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 6, 0, 30))
        painter.drawEllipse(QRectF(60, y + 35, 60, 9))
        painter.save()
        painter.translate(90, y + lift)
        if not self.reduced_motion:
            cursor = self.mapFromGlobal(QCursor.pos())
            lean = max(-6.0, min(6.0, (cursor.x() - 90) / 10)) if self._open else 0.0
            painter.rotate(3 * math.sin(motion + 0.5) + lean)
            painter.scale(1 + 0.018 * math.sin(motion), 1 - 0.015 * math.sin(motion))
        painter.drawPixmap(QRect(-42, -42, 84, 84), self._asset)
        if self._reaction:
            painter.setBrush(QColor(255, 215, 125, 220))
            for x in (-10, 10):
                painter.drawEllipse(QPoint(x, -2), 2, 2)
        painter.restore()
        if self._reaction:
            painter.setPen(Qt.PenStyle.NoPen)
            if self._result and not self._open:
                badge = QRect(35, 46 if y > 80 else 88, 110, 24)
                painter.setBrush(QColor("#171717"))
                painter.drawRoundedRect(badge, 12, 12)
                painter.setPen(QColor("#fff4e9"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._result)
