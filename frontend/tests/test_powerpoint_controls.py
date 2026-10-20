from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QPoint, QPointF, Qt
from PySide6.QtGui import QImage

from buddy_desktop.powerpoint_contract import (
    PowerPointActionResult,
    PowerPointFrame,
    PresentationInfo,
)
from buddy_desktop.powerpoint_controls import PowerPointControls


def info(width=960, height=540):
    return PresentationInfo("lesson.pptx", "lesson.pptx", 1, 4, 42, 256, width, height)


def frame(state):
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return PowerPointFrame(state, bytes(buffer.data()))


@pytest.fixture
def controls(qtbot):
    widget = PowerPointControls()
    qtbot.addWidget(widget)
    widget.resize(900, 700)
    widget.set_info(info())
    widget.set_frame(frame(info()))
    widget.show()
    return widget


@pytest.mark.parametrize("size", [(960, 540), (720, 540), (540, 960)])
@pytest.mark.parametrize("dpr", [1, 1.25, 1.5, 2])
def test_preview_maps_logical_pixels_to_slide_points(controls, monkeypatch, size, dpr):
    state = info(*size)
    controls.set_info(state)
    controls.set_frame(frame(state))
    preview = controls.preview
    preview.resize(800, 600)
    monkeypatch.setattr(preview, "devicePixelRatioF", lambda: dpr)
    point = preview.point(QPointF(400, 300))
    assert point == pytest.approx((size[0] / 2, size[1] / 2))
    scale = min(800 / size[0], 600 / size[1])
    left, top = (800 - size[0] * scale) / 2, (600 - size[1] * scale) / 2
    assert preview.point(QPointF(left + 10 * scale, top + 20 * scale)) == pytest.approx((10, 20))
    assert preview.point(QPointF(-1, -1)) is None
    if left > 0 or top > 0:
        assert preview.point(QPointF(0, 0)) is None  # Reject letterbox areas.
    else:
        assert preview.point(QPointF(0, 0)) == (0, 0)


def test_navigation_emits_typed_requests_without_resetting_edited_target(controls):
    actions = []
    controls.action_requested.connect(actions.append)
    assert not controls.previous.isEnabled()
    controls.next.click()
    assert actions[-1].kind == "next_slide"
    assert actions[-1].target == info()
    controls.target.setValue(3)
    controls.set_info(info())
    assert controls.target.value() == 3
    controls.go.click()
    assert actions[-1].slide_number == 3
    controls.set_info(replace(info(), slide_index=4, slide_id=259))
    assert not controls.next.isEnabled()
    assert controls.previous.isEnabled()


def test_annotation_selection_bounds_and_confirmation(controls, qtbot):
    actions = []
    controls.action_requested.connect(actions.append)
    preview = controls.preview
    # A gesture selects; applying it is explicit, keyboard-accessible, and typed.
    qtbot.mousePress(
        preview, Qt.MouseButton.LeftButton, pos=QPoint(preview.width() // 3, preview.height() // 3)
    )
    qtbot.mouseMove(preview, QPoint(2 * preview.width() // 3, 2 * preview.height() // 3))
    qtbot.mouseRelease(
        preview,
        Qt.MouseButton.LeftButton,
        pos=QPoint(2 * preview.width() // 3, 2 * preview.height() // 3),
    )
    assert actions == []
    assert controls.apply.isEnabled()
    controls.apply.click()
    action = actions[-1]
    action.validate()
    assert action.kind == "highlight_region"
    assert action.region.width > 0 and action.region.height > 0
    controls.completed(PowerPointActionResult(action, info()))
    assert preview.image is not None
    controls.erase.click()
    controls.completed(PowerPointActionResult(actions[-1], info()))
    assert preview.selection is None
    assert controls.info.slide_index == 1
    assert not controls.apply.isEnabled()


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("draw_line", "draw_line"),
        ("draw_arrow", "draw_arrow"),
        ("draw_rectangle", "draw_rectangle"),
        ("draw_oval", "draw_oval"),
        ("draw_underline", "draw_underline"),
        ("draw_freehand", "draw_freehand"),
        ("highlight_region", "highlight_region"),
        ("filled_rectangle", "filled_rectangle"),
        ("filled_oval", "filled_oval"),
        ("text_box", "text_box"),
        ("callout", "callout"),
        ("erase_region", "erase_region"),
        ("pointer_pen", "set_pointer_tool"),
        ("pointer_arrow", "set_pointer_tool"),
    ],
)
def test_every_annotation_tool_builds_a_valid_action(controls, tool, kind):
    controls.mode.setCurrentIndex(controls.mode.findData(tool))
    controls.fields["x1"].setValue(20)
    controls.fields["y1"].setValue(30)
    controls.fields["x2"].setValue(120)
    controls.fields["y2"].setValue(90)
    controls.text_input.setText("Key idea")
    actions = []
    controls.action_requested.connect(actions.append)
    assert controls.apply.isEnabled()
    controls.apply.click()
    assert actions[-1].kind == kind
    actions[-1].validate()
    if tool == "pointer_pen":
        assert actions[-1].pointer_tool == "pen"
    if tool == "pointer_arrow":
        assert actions[-1].pointer_tool == "arrow"
    if tool in {"highlight_region", "filled_rectangle", "filled_oval"}:
        assert actions[-1].color == 0x00FFFF
        assert actions[-1].fill_transparency == pytest.approx(0.62)


def test_preview_is_deliberately_tall_enough_to_show_the_deck_larger(controls):
    assert controls.preview.minimumWidth() >= 420
    assert controls.preview.minimumHeight() >= 480


def test_slide_changes_clear_selection_and_require_new_preview(controls):
    controls.fields["x2"].setValue(200)
    controls.fields["y2"].setValue(100)
    assert controls.apply.isEnabled()
    controls.set_info(replace(info(), slide_index=2, slide_id=257))
    assert controls.preview.selection is None
    assert controls.preview.image.isNull()
    assert not controls.apply.isEnabled()
    controls.set_frame(frame(info()))  # Late image from the previous slide is ignored.
    assert controls.preview.image.isNull()
    assert controls.next.isEnabled()
    controls.set_info(None)
    assert not controls.next.isEnabled()


def test_preview_failure_keeps_navigation_and_retry_available(controls):
    state = replace(info(), slide_index=2, slide_id=257)
    controls.set_info(state)
    controls.set_frame(PowerPointFrame(state, b"invalid image"))
    assert controls.preview.image.isNull()
    assert controls.next.isEnabled()
    assert controls.previous.isEnabled()
    assert not controls.apply.isEnabled()
    requests = []
    controls.preview_requested.connect(requests.append)
    controls.refresh_preview.click()
    assert requests == [state]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows action dispatch")
def test_action_clicked_during_poll_is_executed_once_after_poll(desktop_window, qtbot):
    window = desktop_window()
    page = window.present
    started, release = Event(), Event()
    calls = []

    class FakePowerPoint:
        state = info()

        def list_slideshows(self):
            return (self.state,)

        def status(self, _path):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the status call")
            return self.state

        def execute(self, action):
            calls.append(action)
            self.state = replace(self.state, slide_index=2, slide_id=257)
            return PowerPointActionResult(action, self.state)

        def preview(self, expected):
            return frame(expected)

    page.powerpoint = FakePowerPoint()
    page._attach_powerpoint_info(info())
    page._submit_powerpoint("status", info().path)
    try:
        qtbot.waitUntil(started.is_set)
        page.powerpoint_controls.next.click()
        assert page._queued_powerpoint_action is not None
        assert calls == []
    finally:
        release.set()
    qtbot.waitUntil(lambda: page._powerpoint_info.slide_index == 2)
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)
    assert len(calls) == 1
    assert page._queued_powerpoint_action is None
    assert page.powerpoint_controls.counter.text() == "Slide 2 of 4"
