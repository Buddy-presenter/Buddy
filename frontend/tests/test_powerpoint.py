from pathlib import Path

import pytest

from buddy_desktop.powerpoint import PowerPointAutomation, PowerPointError


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
        return self.items[index - 1]


class FakePresentation:
    def __init__(self, path, *, slides=4):
        self.FullName = str(path)
        self.Name = Path(path).name
        self.Slides = type("Slides", (), {"Count": slides})()
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
        self.View = type(
            "View", (), {"Slide": type("Slide", (), {"SlideIndex": presentation.slide_index})()}
        )()
        self.View.State = 1


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
        status_value = info

        def launch(self, path):
            assert path == str(deck)
            return info

        def list_slideshows(self):
            return (info,)

        def connect(self, path):
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
