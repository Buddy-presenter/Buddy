"""Microphone session gating and bounded in-memory capture regressions."""

import os
import plistlib
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QByteArray, QObject, Qt, Signal

from buddy_desktop import present_widget
from buddy_desktop.microphone import MicrophoneController


class Device:
    def id(self):
        return QByteArray(b"test-mic")

    def description(self):
        return "Test microphone"


class FakeMicrophone(QObject):
    status_changed = Signal(str)
    devices_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.starts = 0
        self.stops = 0

    def inputs(self):
        return [Device()]

    def start(self, _identifier):
        self.starts += 1
        self.enabled = True
        self.status_changed.emit("Microphone on")

    def stop(self):
        self.stops += 1
        self.enabled = False
        self.status_changed.emit("Microphone off")

    def set_muted(self, _value):
        pass

    def set_paused(self, _value):
        pass

    def shutdown(self):
        self.stop()


def test_microphone_never_starts_without_active_session(desktop_window, monkeypatch):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    page = window.present
    page._microphone_toggle()
    assert page.microphone.starts == 0
    assert not page.microphone_button.isEnabled()
    page._session = SimpleNamespace(status="ended")
    page._microphone_toggle()
    assert page.microphone.starts == 0


def test_microphone_stops_when_identity_or_deck_changes(desktop_window, monkeypatch, qtbot):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    page = window.present
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    assert page.microphone.enabled
    page.set_account_identity(None, None)
    assert not page.microphone.enabled
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    assert page.microphone.enabled
    page.load_manifest(Path(__file__).parent / "fixtures/slide_actions/manifest.json")
    assert not page.microphone.enabled
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=10000)


def test_audio_queue_is_bounded_and_cleared_on_stop(qapp, qtbot):
    mic = MicrophoneController()
    try:
        for index in range(8):
            mic.frames.put_nowait(bytes([index]))
        assert mic.frames.full()
        mic.stop()
        assert mic.frames.empty()
        try:
            mic.frames.get_nowait()
            raise AssertionError("Captured audio remained after stopping")
        except Empty:
            pass
    finally:
        mic.shutdown()


def test_late_permission_response_cannot_restart_a_stopped_microphone(qapp, monkeypatch):
    from buddy_desktop import microphone

    class App:
        callback = None

        @staticmethod
        def checkPermission(_permission):
            return Qt.PermissionStatus.Undetermined

        @classmethod
        def requestPermission(cls, _permission, _context, callback):
            cls.callback = callback

    monkeypatch.setattr(microphone, "QCoreApplication", SimpleNamespace(instance=lambda: App()))
    mic = MicrophoneController()
    monkeypatch.setattr(mic, "inputs", lambda: [Device()])
    started = []
    monkeypatch.setattr(mic, "_begin", lambda *_args: started.append(True))
    try:
        mic.start(b"test-mic")
        assert App.callback is not None
        mic.stop()
        App.callback(SimpleNamespace(status=lambda: Qt.PermissionStatus.Granted))
        assert not started and not mic.enabled
    finally:
        mic.shutdown()


def test_denied_permission_never_starts_capture(qapp, monkeypatch):
    from buddy_desktop import microphone

    class App:
        @staticmethod
        def checkPermission(_permission):
            return Qt.PermissionStatus.Denied

    monkeypatch.setattr(microphone, "QCoreApplication", SimpleNamespace(instance=lambda: App()))
    mic = MicrophoneController()
    monkeypatch.setattr(mic, "inputs", lambda: [Device()])
    messages = []
    mic.status_changed.connect(messages.append)
    try:
        mic.start(b"test-mic")
        assert not mic.enabled and not mic.accepting.is_set()
        assert "permission denied" in messages[-1].lower()
    finally:
        mic.shutdown()


def test_ending_a_session_stops_microphone_before_the_server_request(desktop_window, monkeypatch):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    page = window.present
    page._session = SimpleNamespace(status="active")
    page._session_access = "test-access"
    page._microphone_toggle()
    observed = []
    monkeypatch.setattr(
        page, "_submit_session_task", lambda *_args: observed.append(page.microphone.enabled)
    )
    page._end_session()
    assert observed == [False]


def test_hiding_presenter_stops_microphone(desktop_window, monkeypatch):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    assert page.microphone.enabled
    window.navigation.setCurrentRow(0)
    assert not page.microphone.enabled


def test_removing_selected_device_stops_capture(qapp, monkeypatch):
    mic = MicrophoneController()
    try:
        mic._device_id = b"missing"
        mic._enabled = True
        mic.accepting.set()
        monkeypatch.setattr(mic, "inputs", lambda: [])
        mic._devices_changed()
        assert not mic.enabled
        assert not mic.accepting.is_set()
    finally:
        mic.shutdown()


def test_macos_bundle_declares_microphone_usage_and_signing_entitlement(tmp_path, monkeypatch):
    from frontend.scripts.release import build_desktop

    bundle = tmp_path / "Buddy.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    info = contents / "Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleName": "Buddy"}))
    commands = []
    monkeypatch.setattr(
        build_desktop.subprocess, "run", lambda args, **_kwargs: commands.append(args)
    )
    build_desktop.register_macos_callback(bundle, "Developer ID")
    assert "NSMicrophoneUsageDescription" in plistlib.loads(info.read_bytes())
    entitlements = plistlib.loads((tmp_path / "buddy-entitlements.plist").read_bytes())
    assert entitlements["com.apple.security.device.audio-input"] is True
    assert "--entitlements" in commands[0] and "runtime" in commands[0]


@pytest.mark.skipif(not os.environ.get("BUDDY_TEST_MICROPHONE"), reason="Opt-in real microphone")
def test_present_controls_capture_and_stop_real_microphone(desktop_window, qtbot):
    window = desktop_window()
    page = window.present
    window.navigation.setCurrentRow(3)
    pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    page.load_pdf(pdf, deck_id=str(uuid4()), revision="hardware-test")
    qtbot.waitUntil(lambda: not page.executor.busy, timeout=15000)
    assert page.executor.manifest is not None
    # Only the session boundary is substituted; Qt input and the actual widgets are real.
    page._session = SimpleNamespace(status="active")
    page._session_access = "test-access"
    page._refresh()
    index = page.microphone_device.findText(
        os.environ["BUDDY_TEST_MICROPHONE"], Qt.MatchFlag.MatchContains
    )
    assert index >= 0
    page.microphone_device.setCurrentIndex(index)
    counts = []
    page.microphone.audio_received.connect(counts.append)
    page.microphone_button.click()
    qtbot.waitUntil(lambda: bool(counts), timeout=5000)
    assert page.microphone_status.text() == "Microphone on"
    page.enter_fullscreen()
    qtbot.wait(200)
    assert page.microphone.enabled
    if page.fullscreen is not None:
        page.fullscreen.close()
    page.microphone_mute.click()
    qtbot.wait(300)
    muted_count = len(counts)
    qtbot.wait(400)
    assert len(counts) == muted_count
    page.microphone_mute.click()
    qtbot.waitUntil(lambda: len(counts) > muted_count, timeout=5000)
    page.clear_session_binding()
    qtbot.wait(300)
    stopped_count = len(counts)
    qtbot.wait(400)
    assert len(counts) == stopped_count
    assert not page.microphone.enabled
