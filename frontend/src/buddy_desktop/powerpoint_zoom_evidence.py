"""Private slideshow captures for native zoom; slide exports cannot prove viewport changes."""

import importlib
import time
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_zoom import WindowsPowerPointZoom
from buddy_desktop.slide_contract import Region


def capture(info: PresentationInfo, driver: WindowsPowerPointZoom) -> QImage:
    """Capture only the bound foreground slideshow's slide area in physical pixels."""
    gui, api = driver.gui, driver.api
    ui: Any = importlib.import_module("win32ui")
    driver._guard(info)
    with driver._physical_pixels():
        left, top, right, bottom = gui.GetClientRect(info.window_id)
        origin = gui.ClientToScreen(info.window_id, (left, top))
        width, height = right - left, bottom - top
        scale = min(width / info.slide_width, height / info.slide_height)
        width, height = round(info.slide_width * scale), round(info.slide_height * scale)
        x = origin[0] + (right - left - width) // 2
        y = origin[1] + (bottom - top - height) // 2
        if min(width, height) < 100 or width * height > 32_000_000:
            raise PowerPointError("The slideshow capture size is unavailable.")
        # Bound evidence allocations before copying pixels out of GDI. Office's own
        # rendering shares the same worker budget; three native 4K frames do not fit.
        ratio = min(1.0, 1280 / max(width, height))
        capture_width, capture_height = round(width * ratio), round(height * ratio)
        # Move the pointer off the slide and allow the native toolbar to fade.
        original = api.GetCursorPos()
        dc = gui.GetDC(0)
        screen = ui.CreateDCFromHandle(dc)
        memory = screen.CreateCompatibleDC()
        bitmap = ui.CreateBitmap()
        try:
            api.SetCursorPos((x + width - 1, y))
            time.sleep(1)
            driver._guard(info)
            if gui.ClientToScreen(info.window_id, (left, top)) != origin or gui.GetClientRect(
                info.window_id
            ) != (left, top, right, bottom):
                raise PowerPointError("The slideshow moved during capture.")
            bitmap.CreateCompatibleBitmap(screen, capture_width, capture_height)
            previous = memory.SelectObject(bitmap)
            try:
                gui.SetStretchBltMode(memory.GetSafeHdc(), 4)  # HALFTONE
                memory.StretchBlt(
                    (0, 0),
                    (capture_width, capture_height),
                    screen,
                    (x, y),
                    (width, height),
                    0x00CC0020,
                )
                image = QImage(
                    bitmap.GetBitmapBits(True),
                    capture_width,
                    capture_height,
                    QImage.Format.Format_RGB32,
                ).copy()
            finally:
                memory.SelectObject(previous)
            driver._guard(info)
            return image
        finally:
            if gui.GetForegroundWindow() == info.window_id:
                api.SetCursorPos(original)
            gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            screen.DeleteDC()
            gui.ReleaseDC(0, dc)


def png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # PySide 6.12 accepts str here although its generated stub declares bytes.
    if image.isNull() or not image.save(buffer, "PNG"):  # type: ignore[call-overload]
        raise PowerPointError("The slideshow capture could not be encoded.")
    return bytes(buffer.data().data())


def distance(first: QImage, second: QImage) -> float:
    """Bounded RGB mean error, excluding cursor/toolbar edges; not semantic verification."""

    def pixels(image: QImage) -> bytes:
        image = image.copy(
            round(image.width() * 0.05),
            round(image.height() * 0.05),
            round(image.width() * 0.9),
            round(image.height() * 0.85),
        ).scaled(
            96, 54, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        return bytes(image.constBits())

    return sum(abs(a - b) for a, b in zip(pixels(first), pixels(second), strict=True)) / (
        96 * 54 * 3 * 255
    )


def verify_zoom(
    before: QImage, after: QImage, reset: QImage, info: PresentationInfo, region: Region
) -> None:
    """Require a visible fixed-2x viewport transform and a return to the original view."""
    if (
        any(image.isNull() for image in (before, after, reset))
        or before.size() != after.size()
        or before.size() != reset.size()
    ):
        raise PowerPointError("The slideshow geometry changed during zoom validation.")
    cx = min(0.75, max(0.25, (region.x + region.width / 2) / info.slide_width))
    cy = min(0.75, max(0.25, (region.y + region.height / 2) / info.slide_height))
    expected = before.copy(
        round((cx - 0.25) * before.width()),
        round((cy - 0.25) * before.height()),
        round(before.width() / 2),
        round(before.height() / 2),
    )
    unchanged = distance(before, after)
    if unchanged < 0.015 or distance(expected, after) >= unchanged * 0.8:
        raise PowerPointError("The captured slideshow did not confirm native 2x zoom.")
    if distance(before, reset) > 0.02:
        raise PowerPointError("The slideshow did not return to its original full-slide view.")
