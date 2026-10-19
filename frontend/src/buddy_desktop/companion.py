"""Buddy's local, animated companion: bounded movement, gaze and real app feedback."""

from importlib.resources import files
from math import cos, exp, hypot, pi, sin
from random import Random
from time import monotonic

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QEnterEvent,
    QHideEvent,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QToolButton, QWidget


def spring(value: float, velocity: float, target: float, dt: float) -> tuple[float, float]:
    """Exact critically damped spring; retargeting preserves position and velocity."""
    omega = 8.0
    dt = max(0.0, min(dt, 0.05))
    offset = value - target
    impulse = velocity + omega * offset
    decay = exp(-omega * dt)
    return target + (offset + impulse * dt) * decay, (velocity - omega * impulse * dt) * decay


class BuddyAvatar(QAbstractButton):
    """Original-style body art with independently painted glossy eyes and eyelids."""

    dragged = Signal(object)
    released = Signal()
    hovered = Signal(bool)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(90, 86)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Buddy companion")
        self.setAccessibleDescription(
            "Click or press Space to greet Buddy. Drag to reposition him."
        )
        self.setToolTip("Say hello to Buddy · drag to move")
        self.body = QPixmap()
        self.body.loadFromData(
            files("buddy_desktop").joinpath("assets", "buddy-body.png").read_bytes()
        )
        self.gaze = QPointF()
        self.openness = 1.0
        self.mood = "idle"
        self.roll = 0.0
        self.lift = 0.0
        self.squash = 1.0
        self._press: QPointF | None = None
        self._offset = QPointF()
        self._dragging = False

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        shadow = QRadialGradient(QPointF(45, 77), 34)
        shadow.setColorAt(0, QColor(213, 125, 59, 42))
        shadow.setColorAt(1, QColor(213, 125, 59, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(10, 68, 70, 15))
        painter.save()
        painter.translate(45, 43 - self.lift)
        painter.rotate(self.roll)
        squash = self.squash * (0.94 if self.isDown() else 1)
        painter.scale(1 / squash, squash)
        painter.translate(-45, -43)
        body_rect = QRectF(7, 2, 76, 76)
        painter.drawPixmap(body_rect, self.body, QRectF(self.body.rect()))
        for x in (0.367, 0.612):
            center = QPointF(
                body_rect.x() + body_rect.width() * x + self.gaze.x(),
                body_rect.y() + body_rect.height() * 0.519 + self.gaze.y(),
            )
            if self.mood in {"success", "greeting"}:
                path = QPainterPath()
                path.moveTo(center.x() - 3.6, center.y() + 1)
                path.quadTo(center.x(), center.y() - 5.5, center.x() + 3.6, center.y() + 1)
                painter.setPen(
                    QPen(QColor("#20131A"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                continue
            openness = self.openness * (
                0.22
                if self.mood in {"resting", "presenting"}
                else 1.1
                if self.mood == "curious"
                else 1
            )
            height = max(1.4, 13.0 * openness)
            eye = QRectF(center.x() - 3.5, center.y() - height / 2, 7, height)
            gradient = QLinearGradient(eye.topLeft(), eye.bottomRight())
            gradient.setColorAt(0, QColor("#40333C"))
            gradient.setColorAt(0.45, QColor("#1E171F"))
            gradient.setColorAt(1, QColor("#0E090E"))
            painter.setPen(QPen(QColor("#8A451F"), 0.7))
            painter.setBrush(gradient)
            painter.drawEllipse(eye)
            if openness > 0.55:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 245, 231, 220))
                painter.drawEllipse(QRectF(eye.x() + 1, eye.y() + 1.8, 1.6, 3.4))
                painter.setBrush(QColor(255, 235, 220, 35))
                painter.drawEllipse(QRectF(eye.right() - 1.9, center.y() + 1, 0.8, 2))
        painter.restore()
        if self.mood == "working":
            painter.setPen(QPen(QColor("#F6A142"), 1.5))
            painter.drawArc(QRectF(3, 0, 84, 79), int(self.roll * 500) % 5760, 900)
        elif self.mood == "error":
            painter.setPen(QPen(QColor("#F6A142"), 1.5))
            painter.drawText(QRectF(73, 6, 12, 17), Qt.AlignmentFlag.AlignCenter, "!")
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#FFB477"), 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition()
            self._offset = event.position()
            self._dragging = False
        super().mousePressEvent(event)

    def enterEvent(self, event: QEnterEvent) -> None:
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.hovered.emit(False)
        super().leaveEvent(event)

    def cancel_gesture(self) -> None:
        dragging = self._dragging
        self._dragging = False
        self._press = None
        self.setDown(False)
        if dragging:
            self.released.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is not None:
            delta = event.globalPosition() - self._press
            if hypot(delta.x(), delta.y()) >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                self.setDown(False)
                self.dragged.emit(event.globalPosition() - self._offset)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_dragging = self._dragging
        self._press = None
        self._dragging = False
        if was_dragging:
            self.setDown(False)
            self.released.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class CompanionHabitat(QWidget):
    """A reserved roaming lane: Buddy cannot cover a form, slide or app command."""

    resting_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("companionHabitat")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(116)
        self.setAccessibleName("Buddy companion area")
        self.avatar = BuddyAvatar(self)
        self.avatar.clicked.connect(self.greet)
        self.avatar.dragged.connect(self._drag)
        self.avatar.released.connect(self._release)
        self.avatar.hovered.connect(self._hover_changed)
        self.caption = QLabel("Buddy is here. Say hello.", self)
        self.caption.setObjectName("companionCaption")
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.rest_button = QToolButton(self)
        self.rest_button.setCheckable(True)
        self.rest_button.setText("Let Buddy rest")
        self.rest_button.setAccessibleName("Let Buddy rest")
        self.rest_button.setToolTip("Pause wandering and blinking. Click Buddy to wake him.")
        self.rest_button.toggled.connect(self.set_resting)
        self.state = "idle"
        self.resting = False
        self.wandering = True
        self.reduced_motion = False
        self._closing = False
        self._dragging = False
        self._hovered = False
        self._rng = Random()
        self._x = 24.0
        self._velocity = 0.0
        self._target = 24.0
        self._gaze_x = 0.0
        self._gaze_y = 0.0
        self._gaze_vx = 0.0
        self._gaze_vy = 0.0
        self._age = 0.0
        self._next_wander = 1.4
        self._next_blink = 2.0
        self._blink_start = -100.0
        self._greet_until = 0.0
        self._feedback_until = 0.0
        self._last_drag = monotonic()
        self._clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self._sync_pose()

    def bounds(self) -> tuple[float, float]:
        return 18.0, max(18.0, self.width() - self.avatar.width() - 18.0)

    def _clamp(self, value: float) -> float:
        low, high = self.bounds()
        return max(low, min(high, value))

    def set_state(self, state: str) -> None:
        if state not in {"idle", "attention", "working", "success", "error", "presenting"}:
            raise ValueError("Unsupported companion state")
        if state != self.state and state in {"success", "error"}:
            self._feedback_until = self._age + 2.0
        self.state = state
        self._sync_pose()
        self._sync_running()

    def set_reduced_motion(self, reduced: bool) -> None:
        self.reduced_motion = reduced
        self._velocity = 0
        self._gaze_x = self._gaze_y = 0
        self._sync_pose()
        self._sync_running()

    def set_wandering(self, enabled: bool) -> None:
        self.wandering = enabled
        self._target = self._x
        self._velocity = 0
        self._next_wander = self._age + 1.4
        self._sync_running()

    def set_resting(self, resting: bool) -> None:
        changed = self.resting != resting
        self.resting = resting
        self._target = self._x
        self._velocity = 0
        self.rest_button.setChecked(resting)
        self.rest_button.setText("Wake Buddy" if resting else "Let Buddy rest")
        self.rest_button.setAccessibleName(self.rest_button.text())
        self.rest_button.adjustSize()
        self._layout_chrome()
        self._sync_pose()
        self._sync_running()
        if changed:
            self.resting_changed.emit(resting)

    def greet(self) -> None:
        if self.state == "presenting":
            return
        self.set_resting(False)
        self._greet_until = self._age + 1.25
        self._next_wander = self._age + 4.0
        self._target = self._x
        self._sync_pose()
        self._sync_running()

    def _sync_pose(self) -> None:
        state = self.state
        if state in {"success", "error"} and self._age > self._feedback_until:
            state = "idle"
        if self.resting:
            state = "resting"
        elif state == "idle" and self._age < self._greet_until:
            state = "greeting"
        elif state == "idle" and self._hovered:
            state = "curious"
        self.avatar.mood = state
        text = {
            "idle": "Buddy is here. Say hello.",
            "greeting": "A little hello from Buddy.",
            "curious": "Hi there. Click Buddy to say hello.",
            "resting": "Buddy is resting. Click him to wake up.",
            "presenting": "Your stage. Buddy will stay quiet.",
            "attention": "Buddy noticed your presentation.",
            "working": "Buddy is keeping you company while you work.",
            "success": "Your presentation is saved.",
            "error": "Something needs attention. Check the message above.",
        }[state]
        if self.caption.text() != text:
            self.caption.setText(text)
        description = text + " Drag to reposition; Space to say hello."
        if self.avatar.accessibleDescription() != description:
            self.avatar.setAccessibleDescription(description)
        if self.reduced_motion or state in {"resting", "presenting"}:
            self.avatar.roll = self.avatar.lift = 0
            self.avatar.squash = self.avatar.openness = 1
            self.avatar.gaze = QPointF()
        self.avatar.update()

    def _sync_running(self) -> None:
        active = (
            self.isVisible()
            and self.window().isActiveWindow()
            and not self.window().isMinimized()
            and not self._closing
            and not self.reduced_motion
            and not self.resting
            and self.state != "presenting"
        )
        if active and not self.timer.isActive():
            self._clock.start()
            self.timer.start()
        elif not active:
            self.timer.stop()

    def _tick(self) -> None:
        self._sync_running()
        if not self.timer.isActive():
            return
        dt = min(0.05, max(0.001, self._clock.restart() / 1000))
        pointer = QPointF(self.mapFromGlobal(QCursor.pos()))
        self.advance(
            dt,
            pointer
            if self.window().rect().contains(self.window().mapFromGlobal(QCursor.pos()))
            else None,
        )

    def _hover_changed(self, hovered: bool) -> None:
        self._hovered = hovered
        self._next_wander = self._age + 3
        if hovered and not self._dragging:
            self._target = self._x
        self._sync_pose()

    def advance(self, dt: float, pointer: QPointF | None = None) -> None:
        """One bounded animation step, also used by deterministic motion checks."""
        if self.reduced_motion or self.resting or self.state == "presenting" or self._closing:
            return
        dt = max(0, min(dt, 0.05))
        self._age += dt
        if (
            self._age >= self._next_wander
            and self.wandering
            and not self._dragging
            and not self._hovered
        ):
            low, high = self.bounds()
            # Alternate sides to avoid tiny, indecisive trips around one position.
            fraction = (
                self._rng.uniform(0.62, 0.92)
                if self._x < (low + high) / 2
                else self._rng.uniform(0.08, 0.38)
            )
            self._target = low + (high - low) * fraction
            self._next_wander = self._age + self._rng.uniform(5.5, 8.5)
        if pointer is not None and self.wandering and not self._dragging:
            distance = abs(pointer.x() - self._x - 45)
            if 24 <= pointer.y() <= self.height() and distance > 105:
                self._target = self._clamp(pointer.x() - 45)
                self._next_wander = self._age + 5
        if self.state in {"working", "error"}:
            self._target = self._x
        if not self._dragging:
            waypoint = self._x + max(-32.0, min(32.0, self._target - self._x))
            self._x, self._velocity = spring(self._x, self._velocity, waypoint, dt)
            self._x = self._clamp(self._x)
            self.avatar.move(round(self._x), 27)
        desired_x = desired_y = 0.0
        if pointer is not None:
            desired_x = max(-2.4, min(2.4, (pointer.x() - self._x - 45) / 90))
            desired_y = max(-1.6, min(1.6, (pointer.y() - 70) / 100))
        self._gaze_x, self._gaze_vx = spring(self._gaze_x, self._gaze_vx, desired_x, dt)
        self._gaze_y, self._gaze_vy = spring(self._gaze_y, self._gaze_vy, desired_y, dt)
        if self._age >= self._next_blink:
            self._blink_start = self._age
            self._next_blink = self._age + self._rng.uniform(3.7, 6.5)
        blink = (self._age - self._blink_start) / 0.16
        self._sync_pose()
        self.avatar.openness = max(0.05, abs(cos(pi * blink))) if 0 <= blink <= 1 else 1
        self.avatar.gaze = QPointF(self._gaze_x, self._gaze_y)
        self.avatar.roll = max(-5.5, min(5.5, self._velocity / 65))
        self.avatar.lift = min(2.0, abs(self._velocity) / 120)
        if self.avatar.mood in {"greeting", "success"}:
            end = self._greet_until if self.avatar.mood == "greeting" else self._feedback_until
            remaining = max(0, end - self._age)
            self.avatar.lift = 5 * sin(min(1, remaining / 1.25) * pi) ** 2
            self.avatar.squash = 1 - self.avatar.lift / 180
        else:
            self.avatar.squash = 1
        if self.avatar.mood == "working":
            self.avatar.roll = sin(self._age * 2.2) * 3
        self.avatar.update()

    def _drag(self, global_position: QPointF) -> None:
        if self.state == "presenting":
            return
        point = QPointF(self.mapFromGlobal(global_position.toPoint()))
        self._dragging = True
        now = monotonic()
        x = self._clamp(point.x())
        self._velocity = max(-500, min(500, (x - self._x) / max(0.016, now - self._last_drag)))
        self._last_drag = now
        self._x = self._target = x
        self.avatar.move(round(x), max(25, min(30, round(point.y()))))

    def _release(self) -> None:
        self._dragging = False
        self._target = self._clamp(self._x + self._velocity * 0.12)
        self._next_wander = self._age + 5
        if self.reduced_motion or self.resting:
            self._velocity = 0
            self.avatar.move(round(self._x), 27)

    def _layout_chrome(self) -> None:
        self.rest_button.adjustSize()
        self.rest_button.move(max(18, self.width() - self.rest_button.width() - 18), 4)
        self.caption.setGeometry(18, 5, max(1, self.width() - self.rest_button.width() - 54), 20)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_chrome()
        self._x, self._target = self._clamp(self._x), self._clamp(self._target)
        self.avatar.move(round(self._x), 27)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.window().installEventFilter(self)
        self._sync_running()

    def hideEvent(self, event: QHideEvent) -> None:
        self.timer.stop()
        self.avatar.cancel_gesture()
        super().hideEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            self.avatar.cancel_gesture()
            self.timer.stop()
            return False
        if event.type() in {
            QEvent.Type.WindowActivate,
            QEvent.Type.WindowDeactivate,
            QEvent.Type.WindowStateChange,
        }:
            self._sync_running()
        return False

    def shutdown(self) -> None:
        self._closing = True
        self.timer.stop()
