"""Buddy's local visual assets, palette and accessible command styling."""

import sys
from contextlib import suppress
from importlib.resources import files

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QHideEvent,
    QIcon,
    QImage,
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


def interface_fonts() -> dict[str, str]:
    """Crisp platform UI type; Qt renders variable-font weights poorly on Windows."""
    families = set(QFontDatabase.families())
    if sys.platform == "darwin":
        family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
        return {
            "text": f'"{family}"',
            "strong": f'font-family: "{family}"; font-weight: 600',
            "display": f'font-family: "{family}"; font-weight: 700',
        }
    if "Segoe UI Variable Text" in families:
        return {
            "text": '"Segoe UI Variable Text", "Segoe UI"',
            "strong": 'font-family: "Segoe UI Variable Text Semibold", "Segoe UI Semibold"',
            "display": 'font-family: "Segoe UI Variable Display"; font-weight: 600',
        }
    return {
        "text": '"Segoe UI", "Helvetica Neue", sans-serif',
        "strong": 'font-family: "Segoe UI Semibold", "Segoe UI"; font-weight: 600',
        "display": 'font-family: "Segoe UI Semibold", "Segoe UI"; font-weight: 600',
    }


def apply_theme(app: QApplication) -> None:
    # Auxiliary/local launchers must receive Buddy's taskbar identity as well.
    if sys.platform == "win32":
        import ctypes

        with suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("org.buddy.desktop")
    app.setWindowIcon(app_icon())
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#000000"),
        (QPalette.ColorRole.WindowText, "#F3F0E9"),
        (QPalette.ColorRole.Base, "#121212"),
        (QPalette.ColorRole.AlternateBase, "#181818"),
        (QPalette.ColorRole.Text, "#F3F0E9"),
        (QPalette.ColorRole.Button, "#202020"),
        (QPalette.ColorRole.ButtonText, "#F3F0E9"),
        (QPalette.ColorRole.Highlight, "#A94427"),
        (QPalette.ColorRole.HighlightedText, "#FFFFFF"),
        (QPalette.ColorRole.ToolTipBase, "#252525"),
        (QPalette.ColorRole.ToolTipText, "#F3F0E9"),
        (QPalette.ColorRole.PlaceholderText, "#95959F"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#777781"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#777781"))
    app.setPalette(palette)
    fonts = interface_fonts()
    font = QFont(app.font())
    font.setFamilies([name.strip().strip('"') for name in fonts["text"].split(",")])
    # Unhinted, antialiased outlines avoid the jagged GDI-style look at 100% scale.
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    resources = files("buddy_desktop")
    check_path = str(resources.joinpath("assets", "icons", "check.svg")).replace("\\", "/")
    stylesheet = resources.joinpath("theme.qss").read_text(encoding="utf-8")
    for name, value in (
        ("__BUDDY_CHECK_ICON__", check_path),
        ("__FONT_TEXT__", fonts["text"]),
        ("__FONT_STRONG__", fonts["strong"]),
        ("__FONT_DISPLAY__", fonts["display"]),
    ):
        stylesheet = stylesheet.replace(name, value)
    app.setStyleSheet(stylesheet)


# Measured silhouette of assets/buddy.png (1254 px): a disc centred at (625, 627) whose
# 50% alpha edge lies 573-580 px out. Cropping just inside it drops the hard raster rim.
_MASCOT_DISC = QRect(625 - 571, 627 - 571, 1142, 1142)
_MASCOT_CACHE: dict[tuple[int, int], QImage] = {}


def mascot_image(size: int, inset: int) -> QImage:
    """Buddy at exactly size x size device pixels with an analytic, antialiased edge."""
    key = (size, inset)
    if key not in _MASCOT_CACHE:
        source = QImage()
        source.loadFromData(files("buddy_desktop").joinpath("assets", "buddy.png").read_bytes())
        disc = QRect(inset, inset, size - 2 * inset, size - 2 * inset)
        # One high-quality downscale to the final size; nothing is resampled at paint time.
        # The source interior is ~99% opaque, so flatten it; the mask alone sets coverage.
        face = (
            source.copy(_MASCOT_DISC)
            .scaled(
                disc.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            .convertToFormat(QImage.Format.Format_RGB32)
        )
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(disc))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.drawImage(disc.topLeft(), face)
        painter.end()
        _MASCOT_CACHE[key] = image
    return _MASCOT_CACHE[key]


def icon_inset(size: int) -> int:
    return round(size / 32)


def app_icon() -> QIcon:
    """The Buddy mascot at every size the window, taskbar, Dock and switcher ask for."""
    result = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512):
        result.addPixmap(QPixmap.fromImage(mascot_image(size, icon_inset(size))))
    return result


class BuddyMark(QWidget):
    """A real mascot asset; optional subtle activity motion, never a fake listening state."""

    def __init__(self, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAccessibleName("Buddy")
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
        # Render at device pixels (HiDPI included) so the edge is never resampled.
        ratio = self.devicePixelRatioF()
        size = round(self.width() * ratio)
        pixmap = QPixmap.fromImage(mascot_image(size, round(size * 0.06) + round(ratio)))
        pixmap.setDevicePixelRatio(ratio)
        painter.drawPixmap(0, 0, pixmap)
        if self.active:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#FFB45B"))
            start = self.frame * 30 if not self.reduced_motion else 90
            painter.drawArc(self.rect().adjusted(1, 1, -1, -1), start * 16, 90 * 16)
