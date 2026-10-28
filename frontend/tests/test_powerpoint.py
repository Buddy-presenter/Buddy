from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from buddy_desktop.powerpoint import PowerPointAutomation, PowerPointError
from buddy_desktop.powerpoint_contract import PowerPointAction
from buddy_desktop.slide_contract import Region


class FakePythonCom:
    def __init__(self, registered=True):
        self.registered = registered
        self.initialized = 0
        self.uninitialized = 0

    def CLSIDFromProgID(self, _name):
        if not self.registered:
            raise OSError("not registered")

    def CoInitialize(self):
        self.initialized += 1

    def CoUninitialize(self):
        self.uninitialized += 1


class Collection:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        if isinstance(index, str):
            return next(item for item in self.items if item.Name == index)
        return self.items[index - 1]


class FakeShape:
    def __init__(self, kind, args):
        self.kind = kind
        self.args = args
        self.Name = f"Shape_{id(self)}"
        if kind == "line":
            x1, y1, x2, y2 = args
            self.Left, self.Top = min(x1, x2), min(y1, y2)
            self.Width, self.Height = abs(x2 - x1), abs(y2 - y1)
        else:
            _, self.Left, self.Top, self.Width, self.Height = args
        self.Fill = SimpleNamespace(
            ForeColor=SimpleNamespace(RGB=0), Transparency=0, Solid=lambda: None
        )
        self.Shadow = SimpleNamespace(Visible=-1)
        self.Line = SimpleNamespace(
            ForeColor=SimpleNamespace(RGB=0),
            Weight=0,
            Visible=-1,
            EndArrowheadStyle=0,
        )
        self.TextFrame = SimpleNamespace(
            WordWrap=0,
            TextRange=SimpleNamespace(
                Text="", Font=SimpleNamespace(Color=SimpleNamespace(RGB=0), Size=0)
            ),
        )
        self.Callout = SimpleNamespace(Border=False)
        self.TextFrame2 = SimpleNamespace(
            AutoSize=0,
            WordWrap=0,
            VerticalAnchor=0,
            TextRange=SimpleNamespace(BoundHeight=0, BoundWidth=0),
        )

    def Delete(self):
        self.collection.items.remove(self)


class FakeShapes(Collection):
    def _add(self, kind, *args):
        shape = FakeShape(kind, args)
        shape.collection = self
        self.items.append(shape)
        return shape

    def AddLine(self, *args):
        return self._add("line", *args)

    def AddShape(self, *args):
        return self._add("shape", *args)

    def AddTextbox(self, *args):
        return self._add("textbox", *args)

    def AddCallout(self, *args):
        return self._add("callout", *args)


class FakePresentation:
    def __init__(self, path, *, slides=4):
        self.FullName = str(path)
        self.Name = Path(path).name
        self.Slides = Collection(
            [
                type(
                    "Slide",
                    (),
                    {"SlideIndex": i, "SlideID": 255 + i, "Shapes": FakeShapes()},
                )()
                for i in range(1, slides + 1)
            ]
        )
        self.PageSetup = type("PageSetup", (), {"SlideWidth": 960, "SlideHeight": 540})()
        self.SlideShowSettings = self
        self.AdvanceMode = 2
        self.open_args = None
        self.slide_index = 1

    def Run(self):
        # PowerPoint's real Run method does not consistently return its window object.
        return object()


class FakeSlideshow:
    def __init__(self, presentation):
        self.Presentation = presentation
        self.HWND = 42
        self.View = type(
            "View", (), {"Slide": type("Slide", (), {"SlideIndex": presentation.slide_index})()}
        )()
        self.View.State = 1
        self.View.PointerType = 1
        self.View.PointerColor = SimpleNamespace(RGB=0)


class FakeApplication:
    def __init__(self, path=None, *, slides=4):
        self.Visible = False
        self.AutomationSecurity = 1
        self.presentation = FakePresentation(path, slides=slides) if path else None
        self.SlideShowWindows = Collection(
            [FakeSlideshow(self.presentation)] if self.presentation else []
        )
        self.Presentations = self

    def Open(self, path, **kwargs):
        self.presentation = FakePresentation(path)
        self.presentation.open_args = kwargs
        self.SlideShowWindows.items = [FakeSlideshow(self.presentation)]
        return self.presentation


class FakeClient:
    def __init__(self, app):
        self.app = app
        self.dispatch_calls = []

    def Dispatch(self, prog_id):
        self.dispatch_calls.append(prog_id)
        return self.app

    def GetActiveObject(self, prog_id):
        if prog_id != "PowerPoint.Application" or not self.app.SlideShowWindows.Count:
            raise OSError("no running PowerPoint")
        return self.app


@pytest.fixture
def automation(tmp_path):
    file = tmp_path / "sample.pptx"
    file.write_bytes(b"test fixture")
    pythoncom = FakePythonCom()
    app = FakeApplication()
    client = FakeClient(app)
    service = PowerPointAutomation(
        platform_name="win32",
        com_loader=lambda: (pythoncom, client),
        progid_probe=lambda: pythoncom.CLSIDFromProgID("PowerPoint.Application"),
        process_probe=lambda: True,
    )
    return service, file, pythoncom, app, client


def test_launch_opens_read_only_and_starts_slideshow(automation):
    service, file, pythoncom, app, client = automation

    state = service.launch(file)

    assert client.dispatch_calls == ["PowerPoint.Application"]
    assert app.Visible is True
    assert app.AutomationSecurity == 1
    assert app.presentation.AdvanceMode == 1
    assert app.presentation.open_args == {"ReadOnly": -1, "Untitled": 0, "WithWindow": -1}
    assert state.path == str(file.resolve())
    assert state.name == "sample.pptx"
    assert (state.slide_index, state.slide_count) == (1, 4)
    assert service.status(file).identity == state.identity
    assert (pythoncom.initialized, pythoncom.uninitialized) == (2, 2)


def test_launch_uses_returned_slideshow_when_application_enumeration_lags(automation):
    service, file, _pythoncom, app, _client = automation

    class PresentationWithReturnedWindow(FakePresentation):
        def Run(self):
            return FakeSlideshow(self)

    def open_without_registered_window(path, **kwargs):
        app.presentation = PresentationWithReturnedWindow(path)
        app.presentation.open_args = kwargs
        app.SlideShowWindows.items = []
        return app.presentation

    app.Open = open_without_registered_window

    state = service.launch(file)

    assert state.path == str(file.resolve())
    assert (state.slide_index, state.slide_count) == (1, 4)


def test_launch_matches_office_identity_for_synced_cloud_file(automation):
    service, file, _pythoncom, app, _client = automation
    office_path = "https://example.sharepoint.com/personal/user/Documents/sample.pptx"

    def open_cloud_backed_file(_path, **kwargs):
        app.presentation = FakePresentation(office_path)
        app.presentation.open_args = kwargs
        app.SlideShowWindows.items = [FakeSlideshow(app.presentation)]
        return app.presentation

    app.Open = open_cloud_backed_file

    state = service.launch(file)

    assert state.path == office_path
    assert service.status(office_path).identity == state.identity


def test_snapshot_falls_back_to_current_show_position_when_slide_object_is_unavailable(
    automation,
):
    service, file, _pythoncom, _app, _client = automation
    slideshow = FakeSlideshow(FakePresentation(file.resolve(), slides=6))
    slideshow.View.Slide = object()
    slideshow.View.CurrentShowPosition = 3

    state = service._snapshot(slideshow)

    assert state.slide_index == 3
    assert state.slide_count == 6


def test_transient_no_slide_during_active_show_is_not_reported_as_disconnected(automation):
    service, file, _pythoncom, app, _client = automation
    presentation = FakePresentation(file.resolve())
    slideshow = FakeSlideshow(presentation)
    slideshow.View.Slide = type("Slide", (), {"SlideIndex": 0})()
    slideshow.View.CurrentShowPosition = 0
    app.SlideShowWindows.items = [slideshow]

    with pytest.raises(PowerPointError, match="temporarily busy"):
        service.status(file)


def test_list_and_connect_require_matching_exact_deck(automation, tmp_path):
    service, file, _pythoncom, app, _client = automation
    app.presentation = FakePresentation(file.resolve())
    app.SlideShowWindows.items = [FakeSlideshow(app.presentation)]

    assert service.list_slideshows()[0].identity == service.connect(file).identity
    assert service.status(file).slide_index == 1
    assert service.status(tmp_path / "other.pptx") is None
    with pytest.raises(PowerPointError, match="not running"):
        service.connect(tmp_path / "other.pptx")


def test_ambiguous_duplicate_slideshows_are_never_selected(automation):
    service, file, _pythoncom, app, _client = automation
    app.presentation = FakePresentation(file.resolve())
    app.SlideShowWindows.items = [
        FakeSlideshow(app.presentation),
        FakeSlideshow(app.presentation),
    ]

    with pytest.raises(PowerPointError, match="More than one"):
        service.connect(file)
    with pytest.raises(PowerPointError, match="More than one"):
        service.status(file)


def test_no_running_powerpoint_returns_empty_list(automation):
    service, _file, pythoncom, _app, _client = automation
    service._process_probe = lambda: False

    assert service.list_slideshows() == ()
    assert pythoncom.initialized == pythoncom.uninitialized == 0


def test_launch_rejects_missing_and_non_pptx_files(automation, tmp_path):
    service, _file, pythoncom, _app, _client = automation
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF")

    with pytest.raises(PowerPointError, match="existing"):
        service.launch(tmp_path / "missing.pptx")
    with pytest.raises(PowerPointError, match=".pptx"):
        service.launch(pdf)
    assert pythoncom.initialized == 0


def test_non_windows_fails_clearly_before_importing_com(tmp_path):
    file = tmp_path / "deck.pptx"
    file.write_bytes(b"fixture")
    service = PowerPointAutomation(platform_name="darwin")

    with pytest.raises(PowerPointError, match="Windows only"):
        service.launch(file)


def test_missing_powerpoint_registration_is_actionable(automation):
    service, file, _pythoncom, _app, _client = automation
    pythoncom = FakePythonCom(registered=False)
    service = PowerPointAutomation(
        platform_name="win32",
        com_loader=lambda: (pythoncom, None),
        progid_probe=lambda: pythoncom.CLSIDFromProgID("PowerPoint.Application"),
    )

    with pytest.raises(PowerPointError, match="not installed or registered"):
        service.launch(file)
    assert pythoncom.initialized == pythoncom.uninitialized == 0


def test_failed_com_call_still_uninitializes(automation):
    service, file, pythoncom, _app, client = automation

    def fail(_prog_id):
        raise RuntimeError("COM details should not escape")

    client.Dispatch = fail
    with pytest.raises(PowerPointError, match="could not open"):
        service.launch(file)
    assert pythoncom.initialized == pythoncom.uninitialized == 1


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
def test_present_page_launch_connect_and_explicit_reconnect(
    desktop_window, qtbot, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QInputDialog

    from buddy_desktop.powerpoint import PresentationInfo

    window = desktop_window()
    page = window.present
    deck = tmp_path / "lesson.pptx"
    deck.write_bytes(b"fixture")
    info = PresentationInfo(str(deck), "lesson.pptx", 2, 6)

    class FakePowerPoint:
        status_calls = 0
        connect_calls = 0
        status_value = info

        def launch(self, path):
            assert path == str(deck)
            return info

        def list_slideshows(self):
            return (info,)

        def connect(self, path):
            self.connect_calls += 1
            assert path == info.path
            return info

        def status(self, path):
            self.status_calls += 1
            assert path == info.path
            return self.status_value

    fake = FakePowerPoint()
    page.powerpoint = fake
    page._submit_powerpoint("launch", str(deck))
    qtbot.waitUntil(lambda: page._powerpoint_info is not None)
    assert page._powerpoint_path == str(deck)
    assert "lesson.pptx" in page.powerpoint_status.text()
    assert "slide 2 of 6" in page.powerpoint_status.text()
    page._powerpoint_poll.stop()

    selected = []
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *args: (selected.append(args[3][0]) or args[3][0], True),
    )
    page._list_powerpoint_slideshows()
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: page._powerpoint_info is not None)
    assert selected == [info.choice_label]
    qtbot.waitUntil(lambda: fake.connect_calls == 1)
    assert page._powerpoint_path == info.path
    page._powerpoint_poll.stop()

    fake.status_value = None
    page._submit_powerpoint("status", info.path)
    qtbot.waitUntil(lambda: page._powerpoint_info is None)
    assert page._powerpoint_path is None
    assert "Connect again to explicitly select" in page.powerpoint_status.text()
    assert not page._powerpoint_poll.isActive()
    calls_when_lost = fake.status_calls
    qtbot.wait(100)
    assert fake.status_calls == calls_when_lost


def test_powerpoint_takes_available_page_space_when_no_pdf_is_loaded(desktop_window):
    from buddy_desktop.powerpoint import PresentationInfo

    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    assert page.surface.isVisible()
    assert page.pdf_controls.isVisible()

    info = PresentationInfo("C:/presentations/demo.pptx", "demo.pptx", 1, 3, 1, 1, 960, 540)
    page._powerpoint_info = info
    page.powerpoint_controls.set_info(info)
    page._refresh()

    assert page.powerpoint_controls.isVisible()
    assert not page.surface.isVisible()
    assert not page.pdf_controls.isVisible()
    assert not page.target.isVisible()
    assert page.powerpoint_controls.preview.minimumHeight() >= 480


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
def test_area_erase_command_completion_queues_a_fresh_preview(desktop_window, tmp_path):
    from buddy_desktop.powerpoint_contract import (
        PowerPointAction,
        PowerPointActionResult,
        PresentationInfo,
    )
    from buddy_desktop.slide_contract import Region

    page = desktop_window().present
    info = PresentationInfo(str(tmp_path / "lesson.pptx"), "lesson.pptx", 1, 2, 42, 256, 960, 540)
    action = PowerPointAction("erase_region", info, region=Region(0, 0, 20, 20))
    page._powerpoint_info = info
    page._powerpoint_path = info.path
    page._powerpoint_pending[1] = ("command", info.path)

    page._powerpoint_succeeded(1, PowerPointActionResult(action, info))

    assert page._queued_powerpoint_preview == info


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
def test_failed_powerpoint_launch_reenables_controls(desktop_window, qtbot, tmp_path):
    from buddy_desktop.powerpoint import PowerPointError

    window = desktop_window()
    page = window.present
    deck = tmp_path / "broken.pptx"
    deck.write_bytes(b"fixture")

    class FailingPowerPoint:
        def launch(self, _path):
            raise PowerPointError("PowerPoint could not start this presentation.")

        def list_slideshows(self):
            return ()

        def connect(self, _path):
            raise AssertionError("connect should not be called")

        def status(self, _path):
            return None

    page.powerpoint = FailingPowerPoint()
    page._submit_powerpoint("launch", str(deck))
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)

    assert "could not start" in page.powerpoint_status.text()
    assert page.powerpoint_open_button.isEnabled()
    assert page.powerpoint_connect_button.isEnabled()


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
def test_transient_powerpoint_status_error_keeps_connection_and_retries(
    desktop_window, qtbot, tmp_path
):
    from buddy_desktop.powerpoint import PowerPointError, PresentationInfo

    window = desktop_window()
    page = window.present
    deck = tmp_path / "lesson.pptx"
    deck.write_bytes(b"fixture")
    info = PresentationInfo(str(deck), "lesson.pptx", 2, 6)

    class TemporarilyUnavailablePowerPoint:
        def launch(self, _path):
            return info

        def list_slideshows(self):
            return (info,)

        def connect(self, _path):
            return info

        def status(self, _path):
            raise PowerPointError("Cannot read the active PowerPoint slideshow state.")

    page.powerpoint = TemporarilyUnavailablePowerPoint()
    page._attach_powerpoint_info(info)
    page._submit_powerpoint("status", info.path)
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)

    assert page._powerpoint_path == info.path
    assert page._powerpoint_info == info
    assert page._powerpoint_poll.isActive()
    assert page.powerpoint_disconnect_button.isEnabled()
    assert page.powerpoint_status.text() == (
        "Connected to lesson.pptx — slide 2 of 6. PowerPoint is displaying the slideshow."
    )


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
def test_powerpoint_status_poll_does_not_toggle_controls(desktop_window, tmp_path):
    from buddy_desktop.powerpoint import PresentationInfo

    window = desktop_window()
    page = window.present
    info = PresentationInfo(str(tmp_path / "lesson.pptx"), "lesson.pptx", 2, 6)
    page._powerpoint_info = info
    page._powerpoint_path = info.path
    page._powerpoint_pending[99] = ("status", info.path)

    page._refresh()

    assert page.powerpoint_open_button.isEnabled()
    assert page.powerpoint_connect_button.isEnabled()
    assert page.powerpoint_disconnect_button.isEnabled()

    page._powerpoint_pending[99] = ("connect", info.path)
    page._refresh()

    assert not page.powerpoint_open_button.isEnabled()
    assert not page.powerpoint_connect_button.isEnabled()


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
@pytest.mark.parametrize("fails", [False, True])
def test_disconnect_ignores_late_poll_completion(desktop_window, qtbot, tmp_path, fails):
    from buddy_desktop.powerpoint import PresentationInfo

    page = desktop_window().present
    info = PresentationInfo(str(tmp_path / "lesson.pptx"), "lesson.pptx", 2, 6)
    started, release = Event(), Event()

    class BlockedPowerPoint:
        def list_slideshows(self):
            return ()

        def status(self, _path):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the poll")
            if fails:
                raise PowerPointError("PowerPoint is temporarily busy.")
            return info

    page.powerpoint = BlockedPowerPoint()
    page._attach_powerpoint_info(info)
    page._submit_powerpoint("status", info.path)
    try:
        qtbot.waitUntil(started.is_set)
        page.powerpoint_disconnect_button.click()
        disconnected_text = page.powerpoint_status.text()
        assert page._powerpoint_info is None
        assert not page._powerpoint_poll.isActive()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)

    assert page._powerpoint_info is None
    assert page._powerpoint_path is None
    assert not page._powerpoint_poll.isActive()
    assert page.powerpoint_status.text() == disconnected_text


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows PowerPoint UI only")
@pytest.mark.parametrize("action", ["launch", "status"])
def test_window_finishes_closing_after_powerpoint_task(desktop_window, qtbot, tmp_path, action):
    from buddy_desktop.powerpoint import PresentationInfo

    window = desktop_window()
    page = window.present
    info = PresentationInfo(str(tmp_path / "lesson.pptx"), "lesson.pptx", 2, 6)
    started, release = Event(), Event()

    class BlockedPowerPoint:
        def list_slideshows(self):
            return ()

        def launch(self, _path):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the task")
            return info

        status = launch

    page.powerpoint = BlockedPowerPoint()
    if action == "status":
        page._attach_powerpoint_info(info)
    page._submit_powerpoint(action, info.path)
    try:
        qtbot.waitUntil(started.is_set)
        window.close()
        assert window.isVisible()
        assert window._closing
        assert not window.centralWidget().isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.powerpoint_runner.busy)
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)
    assert not page._powerpoint_poll.isActive()


@pytest.fixture
def control_automation(automation):
    service, file, pythoncom, app, client = automation
    app.Open(str(file.resolve()))
    window = app.SlideShowWindows.Item(1)

    class View:
        State = 1
        index = 1
        PointerColor = type("Color", (), {"RGB": 123})()

        def __init__(self):
            self.jumps = []
            self.lines = []
            self.erases = 0

        @property
        def Slide(self):
            return app.presentation.Slides.Item(self.index)

        def GotoSlide(self, index, reset):
            self.jumps.append((index, reset))
            self.index = index

        def DrawLine(self, *points):
            self.lines.append((self.index, points, self.PointerColor.RGB))

        def EraseDrawing(self):
            self.erases += 1
            if self.PointerType == 1:
                self.lines.clear()

    window.View = View()
    return service, file, pythoncom, app, client, window


def test_powerpoint_navigation_boundaries_and_absolute_jumps(control_automation):
    service, file, pythoncom, app, client, window = control_automation
    info = service.status(file)
    with pytest.raises(PowerPointError, match="first or last"):
        service.execute(PowerPointAction("previous_slide", info))
    assert window.View.jumps == []
    info = service.execute(PowerPointAction("next_slide", info)).info
    assert info.slide_index == 2
    info = service.execute(PowerPointAction("go_to_slide", info, slide_number=4)).info
    assert info.slide_index == 4
    with pytest.raises(PowerPointError, match="first or last"):
        service.execute(PowerPointAction("next_slide", info))
    info = service.execute(PowerPointAction("previous_slide", info)).info
    assert info.slide_index == 3
    assert window.View.jumps == [(2, 0), (4, 0), (3, 0)]
    assert pythoncom.initialized == pythoncom.uninitialized


def test_managed_line_and_highlight_shapes_erase_without_stale_ink(control_automation):
    service, file, _, app, _, window = control_automation
    info = service.status(file)
    service.execute(PowerPointAction("draw_line", info, points=((10, 20), (900, 500))))
    slide = app.presentation.Slides.Item(1)
    assert slide.Shapes.Count == 1
    assert slide.Shapes.Item(1).kind == "line"
    assert slide.Shapes.Item(1).args == (10, 20, 900, 500)
    service.execute(PowerPointAction("highlight_region", info, region=Region(20, 30, 40, 50)))
    assert slide.Shapes.Count == 2
    highlight = slide.Shapes.Item(2)
    assert highlight.kind == "shape"
    assert highlight.args == (1, 20, 30, 40, 50)
    assert highlight.Fill.ForeColor.RGB == 0x202020
    assert highlight.Fill.Transparency == 0.62
    for kind in ("erase_drawing", "reset_view"):
        assert service.execute(PowerPointAction(kind, info)).info.slide_index == 1
        assert slide.Shapes.Count == 0
    assert window.View.erases == 2
    assert window.View.jumps == []


@pytest.mark.parametrize(
    ("kind", "kwargs", "expected_shape"),
    [
        ("draw_arrow", {"points": ((10, 20), (100, 120))}, "line"),
        ("draw_rectangle", {"region": Region(20, 30, 40, 50)}, "shape"),
        ("draw_oval", {"region": Region(20, 30, 40, 50)}, "shape"),
        ("draw_underline", {"region": Region(20, 30, 40, 50)}, "line"),
        (
            "draw_freehand",
            {"points": ((10, 20), (20, 25), (45, 70), (60, 80))},
            "line",
        ),
        ("filled_rectangle", {"region": Region(20, 30, 40, 50)}, "shape"),
        ("filled_oval", {"region": Region(20, 30, 40, 50)}, "shape"),
        (
            "text_box",
            {"region": Region(20, 30, 120, 50), "text": "Project update"},
            "textbox",
        ),
        (
            "callout",
            {"region": Region(20, 30, 120, 50), "text": "Key point"},
            "shape",
        ),
    ],
)
def test_each_annotation_tool_creates_a_named_removable_shape(
    control_automation, kind, kwargs, expected_shape
):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    service.execute(PowerPointAction(kind, info, **kwargs))
    shapes = app.presentation.Slides.Item(1).Shapes
    assert shapes.Count >= 1
    assert all(
        shape.Name.startswith(PowerPointAutomation._MANAGED_ANNOTATION_PREFIX)
        for shape in shapes.items
    )
    assert shapes.Item(1).kind == expected_shape
    service.execute(PowerPointAction("erase_drawing", info))
    assert shapes.Count == 0


@pytest.mark.parametrize("pointer_tool,expected", [("arrow", 1), ("pen", 2)])
def test_native_pointer_modes_are_exposed_and_set(pointer_tool, expected, control_automation):
    service, file, _, _, _, window = control_automation
    info = service.status(file)
    service.execute(
        PowerPointAction("set_pointer_tool", info, pointer_tool=pointer_tool, color=0x3D84F6)
    )
    assert window.View.PointerType == expected
    if pointer_tool == "pen":
        assert window.View.PointerColor.RGB == 0x3D84F6


def test_area_eraser_removes_only_buddy_shapes_under_the_selection(control_automation):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    service.execute(PowerPointAction("draw_line", info, points=((10, 10), (40, 40))))
    service.execute(PowerPointAction("draw_rectangle", info, region=Region(200, 200, 40, 40)))
    shapes = app.presentation.Slides.Item(1).Shapes
    assert shapes.Count == 2
    service.execute(PowerPointAction("erase_region", info, region=Region(0, 0, 60, 60)))
    assert shapes.Count == 1
    assert shapes.Item(1).args == (1, 200, 200, 40, 40)
    service.execute(PowerPointAction("undo_annotation", info))
    assert shapes.Count == 0


def test_undo_retains_history_when_office_refuses_to_delete_shape(control_automation):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    service.execute(PowerPointAction("draw_line", info, points=((10, 10), (40, 40))))
    shapes = app.presentation.Slides.Item(1).Shapes
    shape = shapes.Item(1)
    delete = shape.Delete

    def fail_delete():
        raise RuntimeError("Office refused deletion")

    shape.Delete = fail_delete
    with pytest.raises(PowerPointError, match="still tracked"):
        service.execute(PowerPointAction("undo_annotation", info))
    assert shapes.Count == 1

    shape.Delete = delete
    service.execute(PowerPointAction("undo_annotation", info))
    assert shapes.Count == 0


def test_area_erase_reports_delete_failure_and_keeps_remaining_shape_tracked(
    control_automation,
):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    service.execute(PowerPointAction("draw_rectangle", info, region=Region(10, 10, 40, 40)))
    shape = app.presentation.Slides.Item(1).Shapes.Item(1)
    delete = shape.Delete
    shape.Delete = lambda: (_ for _ in ()).throw(RuntimeError("Office refused deletion"))

    with pytest.raises(PowerPointError, match="still tracked"):
        service.execute(PowerPointAction("erase_region", info, region=Region(0, 0, 100, 100)))
    shape.Delete = delete
    service.execute(PowerPointAction("undo_annotation", info))
    assert app.presentation.Slides.Item(1).Shapes.Count == 0


def test_annotation_configuration_failure_rolls_back_already_added_shape(
    control_automation, monkeypatch
):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)

    def fail_after_naming(_shape, _color, *, arrow=False):
        raise RuntimeError("Office rejected line formatting")

    monkeypatch.setattr(PowerPointAutomation, "_configure_line", fail_after_naming)
    with pytest.raises(PowerPointError, match="could not create the annotation"):
        service.execute(PowerPointAction("draw_line", info, points=((10, 10), (40, 40))))
    assert app.presentation.Slides.Item(1).Shapes.Count == 0


def test_annotation_name_failure_rolls_back_newly_added_shape(control_automation, monkeypatch):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)

    def fail_name(_shape):
        raise RuntimeError("Office refused shape naming")

    monkeypatch.setattr(PowerPointAutomation, "_name_shape", fail_name)
    with pytest.raises(PowerPointError, match="could not create the annotation"):
        service.execute(PowerPointAction("draw_rectangle", info, region=Region(10, 10, 40, 40)))
    assert app.presentation.Slides.Item(1).Shapes.Count == 0


def test_erase_returns_native_pen_to_arrow_before_clearing_strokes(control_automation):
    service, file, _, _, _, window = control_automation
    info = service.status(file)
    view = window.View
    view.PointerType = 2
    view.lines.append((1, (10, 10, 50, 50), 0))  # A native slideshow pen stroke.
    service.execute(PowerPointAction("erase_drawing", info))
    assert view.PointerType == 1
    assert view.lines == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "Quit"},
        {"kind": "zoom_region"},
        {"kind": "go_to_slide", "slide_number": 0},
        {"kind": "go_to_slide", "slide_number": True},
        {"kind": "go_to_slide", "slide_number": 5},
        {"kind": "next_slide", "region": Region(0, 0, 1, 1)},
        {"kind": "draw_line", "points": ((0, 0), (961, 500))},
        {"kind": "draw_line", "points": ((0, float("nan")), (10, 10))},
        {"kind": "draw_line", "points": ((0, 0), (0, 0))},
        {"kind": "highlight_region", "region": Region(0, 0, 0, 5)},
        {"kind": "highlight_region", "region": Region(-1, 0, 5, 5)},
        {"kind": "highlight_region", "region": Region(0, 0, 1000, 500)},
        {"kind": "erase_region", "region": Region(0, 0, 0, 10)},
        {"kind": "set_pointer_tool", "pointer_tool": "eraser"},
    ],
)
def test_invalid_action_is_rejected_before_com(control_automation, kwargs):
    service, file, _, _, client, window = control_automation
    info = service.status(file)
    calls = len(client.dispatch_calls)
    with pytest.raises(PowerPointError):
        service.execute(PowerPointAction(target=info, **kwargs))
    assert len(client.dispatch_calls) == calls
    assert window.View.jumps == window.View.lines == []


@pytest.mark.parametrize("change", ["window", "slide", "size", "path", "duplicate", "ended"])
def test_actions_never_mutate_a_stale_or_ambiguous_target(control_automation, change):
    service, file, pythoncom, app, _, window = control_automation
    info = service.status(file)
    if change == "window":
        window.HWND += 1
    elif change == "slide":
        window.View.index = 2
    elif change == "size":
        app.presentation.PageSetup.SlideWidth = 720
    elif change == "path":
        app.presentation.FullName = str(file.parent / "other.pptx")
    elif change == "duplicate":
        app.SlideShowWindows.items.append(window)
    else:
        window.View.State = 5
    with pytest.raises(PowerPointError):
        service.execute(PowerPointAction("draw_line", info, points=((10, 20), (30, 40))))
    assert window.View.jumps == window.View.lines == []
    assert pythoncom.initialized == pythoncom.uninitialized


def test_freehand_stops_and_rolls_back_if_powerpoint_navigates_mid_annotation(control_automation):
    service, file, _, app, _, window = control_automation
    info = service.status(file)
    slide = app.presentation.Slides.Item(1)
    original_add = slide.Shapes.AddLine
    calls = []

    def add_then_navigate(*points):
        calls.append(points)
        shape = original_add(*points)
        if len(calls) == 1:
            window.View.index = 2
        return shape

    slide.Shapes.AddLine = add_then_navigate
    with pytest.raises(PowerPointError, match="changed"):
        service.execute(
            PowerPointAction("draw_freehand", info, points=((10, 10), (20, 20), (30, 35)))
        )
    assert len(calls) == 1
    assert slide.Shapes.Count == 0
    assert window.View.PointerColor.RGB == 123


def test_preview_is_bounded_and_temp_export_is_removed(control_automation):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    exports = []

    def export(path, format, width, height):
        exports.append((Path(path), format, width, height))
        Path(path).write_bytes(b"preview-bytes")

    app.presentation.Slides.Item(1).Export = export
    frame = service.preview(info)
    assert frame.png == b"preview-bytes"
    assert frame.info == info
    path, format, width, height = exports[0]
    assert (format, width, height) == ("PNG", 1280, 720)
    assert not path.parent.exists()


@pytest.mark.parametrize("windowed", [False, True])
@pytest.mark.parametrize("duplicate", [False, True])
def test_native_handle_fallback_rejects_editor_and_ambiguous_windows(
    monkeypatch, windowed, duplicate
):
    from buddy_desktop import powerpoint

    class WithoutHandle:
        @property
        def HWND(self):
            raise OSError("Office rejects HWND")

    candidates = {
        40: ("PPTFrameClass", "sample - PowerPoint"),
        41: ("screenClass", "PowerPoint Slide Show - sample-other - Read-Only"),
        42: (
            "PPTFrameClass" if windowed else "screenClass",
            "PowerPoint Slide Show - sample - Read-Only",
        ),
    }
    if duplicate:
        candidates[43] = candidates[42]
    gui = SimpleNamespace(
        GetClassName=lambda hwnd: candidates[hwnd][0],
        GetWindowText=lambda hwnd: candidates[hwnd][1],
        EnumWindows=lambda callback, context: [callback(hwnd, context) for hwnd in candidates],
    )
    modules = {
        "win32gui": gui,
        "win32process": SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 100)),
    }
    monkeypatch.setattr(
        powerpoint, "importlib", SimpleNamespace(import_module=lambda name: modules[name])
    )
    monkeypatch.setattr(
        powerpoint,
        "psutil",
        SimpleNamespace(
            Process=lambda pid: SimpleNamespace(name=lambda: "POWERPNT.EXE"), Error=OSError
        ),
    )
    if duplicate:
        with pytest.raises(PowerPointError, match="uniquely identify"):
            PowerPointAutomation._window_handle(WithoutHandle(), "sample.pptx")
    else:
        assert PowerPointAutomation._window_handle(WithoutHandle(), "sample.pptx") == 42


def test_com_action_failure_is_sanitized_and_never_replayed(control_automation):
    service, file, pythoncom, app, _, window = control_automation
    info = service.status(file)
    attempts = []

    def fail(*args):
        attempts.append(args)
        raise RuntimeError("private provider details")

    app.presentation.Slides.Item(1).Shapes.AddLine = fail
    with pytest.raises(PowerPointError, match="not retried") as error:
        service.execute(PowerPointAction("draw_line", info, points=((10, 20), (30, 40))))
    assert "private" not in str(error.value)
    assert attempts == [(10, 20, 30, 40)]
    assert pythoncom.initialized == pythoncom.uninitialized


def test_failed_preview_removes_export_workspace(control_automation):
    service, file, _, app, _, _ = control_automation
    info = service.status(file)
    exports = []

    def fail(path, *_args):
        exports.append(Path(path))
        Path(path).write_bytes(b"partial")
        raise RuntimeError("Office export error")

    app.presentation.Slides.Item(1).Export = fail
    with pytest.raises(PowerPointError, match="export the slide preview"):
        service.preview(info)
    assert not exports[0].parent.exists()
