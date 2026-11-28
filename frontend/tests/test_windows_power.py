import sys

import pytest

from buddy_desktop import windows_power


def test_other_platforms_do_nothing(monkeypatch):
    monkeypatch.setattr(windows_power.sys, "platform", "darwin")
    assert windows_power.keep_full_speed(True) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process power throttling")
def test_windows_opts_out_and_restores(caplog):
    # #255: Buddy Mini hides the main window; Windows must not throttle capture.
    assert windows_power.keep_full_speed(True) is True
    assert windows_power.keep_full_speed(False) is True
    assert "power_throttling_disabled applied=True" in caplog.text


def test_microphone_start_and_stop_toggle_throttling(qapp, monkeypatch):
    from types import SimpleNamespace

    from buddy_desktop import microphone

    calls = []
    monkeypatch.setattr(microphone, "keep_full_speed", calls.append)
    mic = microphone.MicrophoneController()
    try:
        device = SimpleNamespace(id=lambda: SimpleNamespace(data=lambda: b"dev"))
        monkeypatch.setattr(mic, "inputs", lambda: [device])
        mic._begin(device, mic._generation)
        mic.stop()
        assert calls == [True, False]
    finally:
        mic.shutdown()
