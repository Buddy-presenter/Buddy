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


@pytest.mark.parametrize(
    "region",
    [
        Region(280, 160, 400, 220),
        Region(180, 95, 600, 350),
        Region(380, 210, 200, 120),
        Region(280, 468, 400, 24),
    ],
)
def test_direct_proof_checks_target_and_reset(region):
    from buddy_desktop.powerpoint_direct_zoom import direct_zoom_plan

    before = scene()
    info = PresentationInfo("test.pptx", "test.pptx", 1, 1, 123, 256, 960, 540)
    plan = direct_zoom_plan(info, region, (0, 0, 960, 540), (960, 540))
    after = before.copy(plan.x, plan.y, round(960 / plan.scale), round(540 / plan.scale)).scaled(
        960, 540, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    verify_zoom(before, after, before, info, region)
    assert png(before).startswith(b"\x89PNG")
    with pytest.raises(PowerPointError):
        verify_zoom(before, before, before, info, region)
    with pytest.raises(PowerPointError):
        verify_zoom(before, after, after, info, region)


def test_native_capture_downsamples_before_allocating_image_buffers(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import buddy_desktop.powerpoint_zoom_evidence as evidence

    allocated = []
    memory = SimpleNamespace(
        SelectObject=lambda bitmap: None,
        GetSafeHdc=lambda: 2,
        StretchBlt=lambda *_args: None,
        DeleteDC=lambda: None,
    )
    screen = SimpleNamespace(CreateCompatibleDC=lambda: memory, DeleteDC=lambda: None)
    bitmap = SimpleNamespace(
        CreateCompatibleBitmap=lambda _screen, w, h: allocated.append((w, h)),
        GetBitmapBits=lambda _: b"\0" * (allocated[0][0] * allocated[0][1] * 4),
        GetHandle=lambda: 3,
    )
    gui = SimpleNamespace(
        GetClientRect=lambda _: (0, 0, 3840, 2160),
        ClientToScreen=lambda _, point: point,
        GetDC=lambda _: 1,
        ReleaseDC=lambda *_: None,
        DeleteObject=lambda _: None,
        GetForegroundWindow=lambda: 123,
        SetStretchBltMode=lambda *_: None,
    )
    driver = SimpleNamespace(
        gui=gui,
        api=SimpleNamespace(GetCursorPos=lambda: (0, 0), SetCursorPos=lambda _: None),
        _guard=lambda _: None,
        _physical_pixels=nullcontext,
    )
    monkeypatch.setattr(
        evidence.importlib,
        "import_module",
        lambda _: SimpleNamespace(
            CreateDCFromHandle=lambda _: screen,
            CreateBitmap=lambda: bitmap,
        ),
    )
    monkeypatch.setattr(evidence.time, "sleep", lambda _: None)
    info = PresentationInfo("test.pptx", "test.pptx", 1, 1, 123, 256, 960, 540)
    image = evidence.capture(info, driver)
    assert allocated == [(1280, 720)]
    assert (image.width(), image.height()) == (1280, 720)
