"""A separate desktop surface for the current presentation session."""

import math
from importlib.resources import files

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
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

    def __init__(self, mic: QPushButton, pause: QPushButton, undo: QPushButton) -> None:
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
        self._state = "Mic off"
        self.reduced_motion = False
        self._drag: QPoint | None = None
        self._dragged = False
        self._open = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._chime: QSoundEffect | None = None
        self._chime_tail = QTimer(self, singleShot=True, interval=300)
        self._chime_tail.timeout.connect(self.chime_finished)
        self.mascot = QPushButton(self)
        self.mascot.setGeometry(58, 79, 64, 64)
        self.mascot.setAccessibleName("Buddy Mini controls")
        self.mascot.setToolTip("Buddy Mini controls. Drag to reposition. Escape opens Buddy.")
        self.mascot.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 32px; }"
            "QPushButton:focus { border: 2px solid #ff9f61; }"
        )
        self.mascot.setMouseTracking(True)
        self.mascot.installEventFilter(self)
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
            ("pause", "Pause or resume automatic moves", pause.click),
            ("rotate-ccw", "Undo automatic move", undo.click),
            ("settings-2", "Settings", self.settings_requested.emit),
            ("external-link", "Open Buddy", self.restore_requested.emit),
        )
        self.buttons = []
        for index, (glyph, name, callback) in enumerate(controls):
            button = QPushButton(self)
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setIcon(icon(glyph))
            button.setFixedSize(32, 32)
            button.setStyleSheet(
                "QPushButton { background: #171717; border: 1px solid #666; border-radius: 16px; }"
                "QPushButton:hover, QPushButton:focus { border: 2px solid #ff9f61; }"
                "QPushButton:disabled { background: #393939; border-color: #393939; }"
            )
            angle = math.radians(194 + index * 38)
            button.move(round(74 + 68 * math.cos(angle)), round(95 + 68 * math.sin(angle)))
            button.clicked.connect(callback)
            self.buttons.append(button)
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

    def enterEvent(self, event) -> None:
        self._set_controls(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not any(button.hasFocus() for button in self.buttons):
            self._set_controls(False)
        super().leaveEvent(event)

    def eventFilter(self, watched, event) -> bool:
        from PySide6.QtCore import QEvent

        if watched is self.mascot and isinstance(event, QMouseEvent):
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._drag = event.globalPosition().toPoint() - self.pos()
                self._dragged = False
            elif event.type() == QEvent.Type.MouseMove and self._drag is not None:
                target = event.globalPosition().toPoint() - self._drag
                if self._dragged or (target - self.pos()).manhattanLength() > 5:
                    area = self.screen().availableGeometry()
                    self.move(
                        max(area.left(), min(target.x(), area.right() - self.width() + 1)),
                        max(area.top(), min(target.y(), area.bottom() - self.height() + 1)),
                    )
                    self._dragged = True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._drag = None
                if self._dragged:
                    self.mascot.setDown(False)
                    return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.restore_requested.emit()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.closed.emit()
        super().hideEvent(event)

    def sync(self, state: str, mic: QPushButton, pause: QPushButton, undo: QPushButton) -> None:
        self._listening = state == "Listening"
        self._state = state
        self.mascot.setAccessibleName(f"Buddy Mini — {state}")
        self.mascot.setToolTip(f"{state}. Click for controls or drag to reposition.")
        for mini, full in zip(self.buttons, (mic, pause, undo), strict=False):
            mini.setEnabled(full.isEnabled())
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
        if self.isVisible() and (self._reaction or (self._listening and not self.reduced_motion)):
            self._phase = (self._phase + 1) % 36
            self._reaction = max(0, self._reaction - 1)
            if not self._reaction and self._result:
                self._result = ""
                self.mascot.setAccessibleDescription("")
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._listening:
            for offset in (0, 12):
                phase = 12 if self.reduced_motion else (self._phase + offset) % 24
                radius = 34 + phase // 2
                painter.setPen(QPen(QColor(255, 164, 80, 180 - phase * 5), 1.6))
                painter.drawEllipse(self._center, radius, radius)
        painter.drawPixmap(QRect(57, 78, 66, 66), self._asset)
        painter.setPen(QPen(QColor("#171717"), 1))
        painter.setBrush(
            QColor(
                "#ffb977" if self._listening else "#e8e8e8" if self._state == "Muted" else "#777777"
            )
        )
        painter.drawEllipse(QPoint(116, 136), 4, 4)
        if self._reaction:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 215, 125, 220))
            for x in (82, 98):
                painter.drawEllipse(QPoint(x, 109), 2, 2)
            if self._result and not self._open:
                badge = QRect(35, 46, 110, 24)
                painter.setBrush(QColor("#171717"))
                painter.drawRoundedRect(badge, 12, 12)
                painter.setPen(QColor("#fff4e9"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._result)
