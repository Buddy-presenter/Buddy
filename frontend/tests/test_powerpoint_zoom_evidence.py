import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from buddy_desktop.powerpoint_contract import PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_zoom_evidence import png, verify_zoom
from buddy_desktop.slide_contract import Region


def scene():
    image = QImage(960, 540, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    for x, y, color in ((40, 70, "red"), (350, 200, "blue"), (650, 380, "black")):
        painter.fillRect(QRect(x, y, 140, 100), QColor(color))
    painter.end()
    return image


def test_zoom_proof_requires_actual_two_times_transform_and_reset():
    before = scene()
    info = PresentationInfo("test.pptx", "test.pptx", 1, 1, 123, 256, 960, 540)
    region = Region(400, 220, 160, 100)
    after = before.copy(240, 135, 480, 270).scaled(
        960, 540, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    verify_zoom(before, after, before, info, region)
    assert png(before).startswith(b"\x89PNG")
    with pytest.raises(PowerPointError, match="2x"):
        verify_zoom(before, before, before, info, region)
    with pytest.raises(PowerPointError, match="original"):
        verify_zoom(before, after, after, info, region)
    unrelated = QImage(before)
    unrelated.fill(QColor("orange"))
    with pytest.raises(PowerPointError, match="2x"):
        verify_zoom(before, unrelated, before, info, region)
    with pytest.raises(PowerPointError, match="geometry"):
        verify_zoom(before, after.scaled(480, 270), before, info, region)
