from PySide6.QtWidgets import QWidget

from buddy_desktop import window_focus


class FakeUser32:
    def __init__(self, *, grant_focus: bool = True, grant_after_attach: bool = False):
        self.grant_focus = grant_focus
        self.grant_after_attach = grant_after_attach
        self.attached = False
        self.foreground = 0
        self.attach_calls: list[bool] = []

    def AllowSetForegroundWindow(self, process_id: int) -> bool:
        return process_id > 0

    def IsIconic(self, _handle) -> bool:
        return False

    def ShowWindow(self, _handle, _command: int) -> bool:
        return True

    def BringWindowToTop(self, _handle) -> bool:
        return True

    def SetForegroundWindow(self, handle) -> bool:
        if self.grant_focus or (self.attached and self.grant_after_attach):
            self.foreground = handle.value
            return True
        return False

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def GetWindowThreadProcessId(self, handle, _process_id) -> int:
        return int(handle.value if hasattr(handle, "value") else handle)

    def AttachThreadInput(self, _source: int, _target: int, attach: bool) -> bool:
        self.attach_calls.append(attach)
        self.attached = attach
        return True


def test_allow_foreground_permission_reports_windows_result(monkeypatch):
    fake_user32 = FakeUser32()
    monkeypatch.setattr(window_focus.sys, "platform", "win32")
    monkeypatch.setattr(window_focus, "_user32", lambda: fake_user32)

    assert window_focus.allow_current_process_foreground()


def test_qt_activation_reports_if_windows_granted_foreground(qtbot, monkeypatch):
    fake_user32 = FakeUser32()
    monkeypatch.setattr(window_focus.sys, "platform", "win32")
    monkeypatch.setattr(window_focus, "_user32", lambda: fake_user32)
    window = QWidget()
    qtbot.addWidget(window)
    window.show()

    assert window_focus.activate_qt_window(window)


def test_qt_activation_surfaces_windows_foreground_denial(qtbot, monkeypatch):
    fake_user32 = FakeUser32(grant_focus=False)
    monkeypatch.setattr(window_focus.sys, "platform", "win32")
    monkeypatch.setattr(window_focus, "_user32", lambda: fake_user32)
    window = QWidget()
    qtbot.addWidget(window)
    window.show()

    assert not window_focus.activate_qt_window(window)


def test_qt_activation_detaches_input_queues_after_fallback_success(qtbot, monkeypatch):
    fake_user32 = FakeUser32(grant_focus=False, grant_after_attach=True)
    fake_user32.foreground = 99
    monkeypatch.setattr(window_focus.sys, "platform", "win32")
    monkeypatch.setattr(window_focus, "_user32", lambda: fake_user32)
    window = QWidget()
    qtbot.addWidget(window)
    window.show()

    assert window_focus.activate_qt_window(window)
    assert fake_user32.attach_calls == [True, False]


def test_qt_activation_detaches_input_queues_after_fallback_failure(qtbot, monkeypatch):
    fake_user32 = FakeUser32(grant_focus=False)
    fake_user32.foreground = 99
    monkeypatch.setattr(window_focus.sys, "platform", "win32")
    monkeypatch.setattr(window_focus, "_user32", lambda: fake_user32)
    window = QWidget()
    qtbot.addWidget(window)
    window.show()

    assert not window_focus.activate_qt_window(window)
    assert fake_user32.attach_calls == [True, False]
