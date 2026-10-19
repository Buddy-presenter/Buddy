"""Buddy's local visual assets, palette and accessible command styling."""

from importlib.resources import files
from math import sin

from PySide6.QtCore import (
    Property,
    QByteArray,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QHideEvent,
    QIcon,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPixmap,
    QShowEvent,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QPushButton, QWidget


def icon(name: str, color: str = "#B6B6C0") -> QIcon:
    data = files("buddy_desktop").joinpath("assets", "icons", name + ".svg").read_bytes()
    renderer = QSvgRenderer(QByteArray(data))
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pixmap.rect(), QColor(color))
    painter.end()
    return QIcon(pixmap)


def command(
    button: QPushButton, name: str, *, primary: bool = False, compact: bool = False
) -> None:
    label = button.text().replace("&", "").replace("...", "").replace("\u2026", "")
    button.setAccessibleName(button.accessibleName() or label)
    button.setToolTip(label or button.accessibleName())
    button.setIcon(icon(name, "#17100C" if primary else "#C7C7CF"))
    button.setIconSize(QSize(18, 18))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setProperty("primary", primary)
    if compact:
        button.setText("")
        button.setFixedSize(38, 38)
        button.setProperty("iconOnly", True)


def apply_theme(app: QApplication) -> None:
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#0B0C10"),
        (QPalette.ColorRole.WindowText, "#F3F0E9"),
        (QPalette.ColorRole.Base, "#121318"),
        (QPalette.ColorRole.AlternateBase, "#18191F"),
        (QPalette.ColorRole.Text, "#F3F0E9"),
        (QPalette.ColorRole.Button, "#202128"),
        (QPalette.ColorRole.ButtonText, "#F3F0E9"),
        (QPalette.ColorRole.Highlight, "#A94427"),
        (QPalette.ColorRole.HighlightedText, "#FFFFFF"),
        (QPalette.ColorRole.ToolTipBase, "#25262E"),
        (QPalette.ColorRole.ToolTipText, "#F3F0E9"),
        (QPalette.ColorRole.PlaceholderText, "#95959F"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#777781"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#777781"))
    app.setPalette(palette)
    resources = files("buddy_desktop")
    check_path = str(resources.joinpath("assets", "icons", "check.svg")).replace("\\", "/")
    stylesheet = resources.joinpath("theme.qss").read_text(encoding="utf-8")
    app.setStyleSheet(stylesheet.replace("__BUDDY_CHECK_ICON__", check_path))


class BuddyMark(QWidget):
    """The original character, animated from real desktop events, without replacing its art."""

    def __init__(self, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAccessibleName("Buddy")
        self.image = QPixmap()
        self.image.loadFromData(files("buddy_desktop").joinpath("assets", "buddy.png").read_bytes())
        self.frame = 0
        self.active = False
        self.reduced_motion = False
        self.state = "idle"
        self._pose = 0.0
        self._pointer = 0.0
        self.setMouseTracking(True)
        self.setToolTip("Buddy is ready")
        self.animation = QPropertyAnimation(self, b"pose", self)
        self.animation.setDuration(260)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)

    def set_activity(self, active: bool) -> None:
        self.active = active
        self.set_state("working" if active else "idle")

    def set_state(self, state: str) -> None:
        if state not in {"idle", "attention", "working", "success", "error", "presenting"}:
            raise ValueError("Unsupported Buddy state")
        changed = self.state != state
        self.state = state
        self.active = state == "working"
        labels = {
            "idle": "Buddy is ready",
            "attention": "Buddy is ready to import",
            "working": "Buddy is working",
            "success": "Presentation saved",
            "error": "Buddy needs your attention",
            "presenting": "Buddy is staying quiet",
        }
        self.setAccessibleDescription(labels[state])
        self.setToolTip(labels[state])
        if changed or self.reduced_motion:
            self.animation.stop()
            target = {"attention": -7.0, "success": 6.0, "error": -4.0}.get(state, 0.0)
            if self.reduced_motion or not self.isVisible():
                self._pose = target
            else:
                self.animation.setStartValue(self._pose)
                self.animation.setEndValue(target)
                self.animation.start()
        self._sync_timer()
        self.update()

    def _get_pose(self) -> float:
        return self._pose

    def _set_pose(self, value: float) -> None:
        self._pose = value
        self.update()

    pose = Property(float, _get_pose, _set_pose)

    def _sync_timer(self) -> None:
        active = self.active
        if active and not self.reduced_motion and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % 180
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self.reduced_motion and self.state in {"idle", "attention"}:
            self._pointer = (event.position().x() / max(1, self.width()) - 0.5) * 5
            self.update()

    def leaveEvent(self, event: object) -> None:
        self._pointer = 0
        self.update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event: QHideEvent) -> None:
        self.timer.stop()
        self.animation.stop()
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        painter.translate(center)
        angle = self._pose
        if not self.reduced_motion and self.active:
            angle += sin(self.frame / 9) * 2
        painter.rotate(angle + (0 if self.reduced_motion else self._pointer))
        painter.translate(-center)
        inset = max(3, self.width() * 0.06)
        target = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawPixmap(target, self.image, QRectF(self.image.rect()))
        painter.resetTransform()
        if self.active:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#FFB45B"))
            start = self.frame * 8 if not self.reduced_motion else 90
            painter.drawArc(self.rect().adjusted(1, 1, -1, -1), start * 16, 90 * 16)
        elif self.state in {"success", "error"}:
            # Shape and accessible description also convey the result without color.
            painter.setPen(QColor("#FFB477"))
            x, y = self.width() - 10, self.height() - 10
            if self.state == "success":
                painter.drawLine(x - 5, y, x - 2, y + 3)
                painter.drawLine(x - 2, y + 3, x + 4, y - 4)
            else:
                painter.drawText(x - 3, y + 4, "!")
