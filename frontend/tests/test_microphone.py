"""Microphone session gating and bounded in-memory capture regressions."""

import os
import plistlib
from datetime import UTC
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QByteArray, QObject, Qt, Signal

from buddy_desktop import present_widget
from buddy_desktop.microphone import MicrophoneController
from buddy_desktop.powerpoint import PresentationInfo


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
    monkeypatch.setattr(page, "_microphone_session_available", lambda: True)
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
    monkeypatch.setattr(page, "_microphone_session_available", lambda: True)
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
    monkeypatch.setattr(page, "_microphone_session_available", lambda: True)
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    assert page.microphone.enabled
    window.navigation.setCurrentRow(0)
    assert not page.microphone.enabled


@pytest.mark.parametrize("end", ["disconnect", "lost", "changed", "status_failure", "timeout"])
def test_powerpoint_microphone_stops_when_connection_is_lost(desktop_window, monkeypatch, end):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    info = PresentationInfo("C:/slides/demo.pptx", "demo.pptx", 1, 2, 42, 256, 960, 540)
    page._attach_powerpoint_info(info)
    page._refresh()
    assert page.microphone_panel.isVisible()
    assert page.microphone_button.isEnabled()
    page.microphone_button.click()
    assert page.microphone.enabled
    if end == "disconnect":
        page._disconnect_powerpoint()
    elif end == "lost":
        page._powerpoint_lost()
        page._refresh()
    elif end == "changed":
        page._attach_powerpoint_info(
            PresentationInfo("C:/slides/other.pptx", "other.pptx", 1, 2, 43, 257, 960, 540)
        )
        page._refresh()
    elif end == "status_failure":
        page._powerpoint_pending[123] = ("status", info.path)
        page._powerpoint_failed(123, "PowerPoint unavailable")
    else:
        page._powerpoint_verification_expired()
    assert not page.microphone.enabled
    if end in {"disconnect", "lost", "status_failure", "timeout"}:
        assert not page.microphone_button.isEnabled()
    if end in {"status_failure", "timeout"}:
        page._attach_powerpoint_info(info)
        page._refresh()
        assert page.microphone_button.isEnabled()
        assert not page.microphone.enabled


def test_powerpoint_slide_change_keeps_microphone_running(desktop_window, monkeypatch):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    window.navigation.setCurrentRow(3)
    page = window.present
    page._attach_powerpoint_info(
        PresentationInfo("C:/slides/demo.pptx", "demo.pptx", 1, 2, 42, 256, 960, 540)
    )
    page._refresh()
    page.microphone_button.click()
    page._attach_powerpoint_info(
        PresentationInfo("C:/slides/demo.pptx", "demo.pptx", 2, 2, 42, 257, 960, 540)
    )
    assert page.microphone.enabled
    page._disconnect_powerpoint()


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


def test_old_worker_failure_cannot_stop_new_capture(qapp):
    mic = MicrophoneController()
    try:
        mic._generation = 10
        mic._enabled = True
        mic.accepting.set()
        mic._worker_changed(9, "Microphone stopped. Check device and system permissions.")
        assert mic.enabled and mic.accepting.is_set()
        mic._worker_changed(10, "Microphone stopped. Check device and system permissions.")
        assert not mic.enabled and not mic.accepting.is_set()
    finally:
        mic.shutdown()


def test_pdf_microphone_stops_when_remote_session_ends(desktop_window, monkeypatch):
    from datetime import datetime

    from buddy_desktop.presentation_session_service import PresentationSession

    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    page = window.present
    monkeypatch.setattr(page, "_microphone_session_available", lambda: True)
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    page._microphone_pdf_request_generation = page._microphone_pdf_generation
    page._session_request = ("microphone_verify", page._session_generation, None)
    ended = PresentationSession("s", "o", "d", "a", "ended", 1, None, datetime.now(UTC))
    page._session_succeeded(1, ("success", ended, None))
    assert not page.microphone.enabled
    assert page._session is None and page._session_ended
    assert "could not be verified" in page.microphone_status.text()


def test_pdf_microphone_stops_when_session_verification_times_out(desktop_window, monkeypatch):
    monkeypatch.setattr(present_widget, "MicrophoneController", FakeMicrophone)
    window = desktop_window()
    page = window.present
    monkeypatch.setattr(page, "_microphone_session_available", lambda: True)
    page._session = SimpleNamespace(status="active")
    page._microphone_toggle()
    page._microphone_pdf_unverified()
    assert not page.microphone.enabled
