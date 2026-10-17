"""Real raster slide surface and deterministic canonical-to-viewport mapping."""

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QThread
from PySide6.QtGui import QColor, QImage, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from buddy_desktop.slide_contract import Region, Slide


@dataclass(frozen=True)
class Mapping:
    scale: float
    offset_x: float
    offset_y: float
    device_ratio: float

    def point(self, x: float, y: float, *, device: bool = False) -> QPointF:
        ratio = self.device_ratio if device else 1.0
        return QPointF(
            (self.offset_x + x * self.scale) * ratio, (self.offset_y + y * self.scale) * ratio
        )


def viewport_mapping(region: Region, width: float, height: float, dpr: float = 1) -> Mapping:
    """Qt uses logical pixels; multiply the *entire* mapping by DPR for device pixels."""
    import math

    values = (region.x, region.y, region.width, region.height, width, height, dpr)
    if not all(math.isfinite(value) for value in values) or min(values[2:]) <= 0:
        raise ValueError("Mapping needs finite positive dimensions and device ratio.")
    scale = min(width / region.width, height / region.height)
    return Mapping(
        scale,
        (width - region.width * scale) / 2 - region.x * scale,
        (height - region.height * scale) / 2 - region.y * scale,
        dpr,
    )


class SlideSurface(QWidget):
    """A bounded camera: contain the requested region, clip to the slide, preserve aspect."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Slide presentation")
        self.setMinimumSize(1, 1)
        self._image = QImage()
        self._slide: Slide | None = None
        self._region: Region | None = None
        self._highlight: Region | None = None
        self.setAutoFillBackground(False)

    def check_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Slide state must only be accessed on its Qt thread.")

    @property
    def region(self) -> Region | None:
        return self._region

    @property
    def slide(self) -> Slide | None:
        return self._slide

    @property
    def highlight(self) -> Region | None:
        return self._highlight

    def commit(self, slide: Slide, image: QImage, region: Region) -> None:
        self.check_thread()
        self._slide, self._image, self._region = slide, image, region
        self._highlight = None
        self.setAccessibleDescription(f"Slide {slide.slide_id}")
        self.update()

    def reset(self) -> None:
        self.check_thread()
        if self._slide is not None:
            self._region = self._slide.full
            self._highlight = None
            self.update()

    def clear(self) -> None:
        self.check_thread()
        self._image = QImage()
        self._slide = self._region = self._highlight = None
        self.update()

    def set_highlight(self, region: Region) -> None:
        self.check_thread()
        self._highlight = region
        self.update()

    def set_region(self, region: Region) -> None:
        self.check_thread()
        self._region = region
        self.update()

    def mapping(self) -> Mapping | None:
        if self._region is None or self.width() <= 0 or self.height() <= 0:
            return None
        return viewport_mapping(self._region, self.width(), self.height(), self.devicePixelRatioF())

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#06090D"))
        mapping = self.mapping()
        if mapping is None or self._slide is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(mapping.offset_x, mapping.offset_y)
        painter.scale(mapping.scale, mapping.scale)
        painter.drawImage(QRectF(0, 0, self._slide.width, self._slide.height), self._image)
        if self._highlight is not None:
            highlight = self._highlight
            color = QColor("#FFEB3B")
            color.setAlpha(90)
            painter.fillRect(
                QRectF(highlight.x, highlight.y, highlight.width, highlight.height), color
            )
