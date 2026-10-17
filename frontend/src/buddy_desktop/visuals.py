"""Buddy's local visual assets, palette and accessible command styling."""

from importlib.resources import files

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QHideEvent,
    QIcon,
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
    app.setStyleSheet(files("buddy_desktop").joinpath("theme.qss").read_text(encoding="utf-8"))


class BuddyMark(QWidget):
    """A real mascot asset; optional subtle activity motion, never a fake listening state."""

    def __init__(self, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAccessibleName("Buddy")
        self.image = QPixmap()
        self.image.loadFromData(files("buddy_desktop").joinpath("assets", "buddy.png").read_bytes())
        self.frame = 0
        self.active = False
        self.reduced_motion = False
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._tick)

    def set_activity(self, active: bool) -> None:
        self.active = active
        self.timer.setInterval(120)
        if active and not self.reduced_motion and self.isVisible():
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % 12
        self.update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.set_activity(self.active)

    def hideEvent(self, event: QHideEvent) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.drawPixmap(target, self.image, QRectF(self.image.rect()))
        if self.active:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#FFB45B"))
            start = self.frame * 30 if not self.reduced_motion else 90
            painter.drawArc(self.rect().adjusted(1, 1, -1, -1), start * 16, 90 * 16)
