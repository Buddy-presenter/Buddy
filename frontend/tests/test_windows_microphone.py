"""Exact endpoint selection and lifetime of opt-in Windows capture."""

from types import SimpleNamespace

import pytest

from buddy_desktop import windows_microphone as capture


def device(**overrides):
    return dict(max_input_channels=2, hostapi=0, default_samplerate=48000, **overrides)


def test_selection_uses_endpoint_identity_even_with_identical_names():
    devices = [device(), device()]
    assert capture.select_input(
        b"SECOND", devices, [{"name": "Windows WASAPI"}], lambda i: ["first", "second"][i]
    ) == (1, 48000, 2)


@pytest.mark.parametrize("rate", [0, 7999, 96001, float("nan"), float("inf"), 48000.5])
def test_selection_rejects_invalid_native_format(rate):
    source = device()
    source["default_samplerate"] = rate
    with pytest.raises(ValueError, match="Unsupported"):
        capture.select_input(b"id", [source], [{"name": "Windows WASAPI"}], lambda _: "id")


def test_selection_never_substitutes_another_api_or_ambiguous_device():
    for devices, apis in [
        ([device()], [{"name": "MME"}]),
        ([device(), device()], [{"name": "Windows WASAPI"}]),
        ([], []),
    ]:
        with pytest.raises(ValueError, match="unavailable or ambiguous"):
            capture.select_input(b"id", devices, apis, lambda _: "id")


@pytest.fixture
def sdk(monkeypatch):
    calls = []

    class Stream:
        active = False

        def __init__(self, **kwargs):
            self.options = kwargs
            calls.append("open")

        def start(self):
            self.active = True

        def abort(self):
            self.active = False
            calls.append("abort")

        def close(self):
            calls.append("close")

    fake = SimpleNamespace(
        _libname="pinned-portaudio",
        _terminate=lambda: calls.append("terminate"),
        _initialize=lambda: calls.append("initialize"),
        query_devices=lambda: [device()],
        query_hostapis=lambda: [{"name": "Windows WASAPI"}],
        WasapiSettings=lambda **kwargs: kwargs,
        RawInputStream=Stream,
    )
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: fake)
    monkeypatch.setattr(capture, "EndpointLookup", lambda _: lambda _: "id")
    return fake, calls


def test_exclusive_capture_downmixes_and_ignores_callbacks_after_close(sdk):
    _, calls = sdk
    received, gaps = [], []
    mic = capture.WindowsInput(b"id", received.append, lambda: gaps.append(True))
    try:
        assert calls == ["terminate", "initialize", "open"]
        options = mic.stream.options
        assert options["extra_settings"] == {"exclusive": True}
        assert options["channels"] == 2 and options["samplerate"] == 48000
        mic.start()
        assert mic.active
        callback = options["callback"]
        callback(b"\x00\x10\x00\x20", 1, None, True)
        assert received == [b"\x00\x18"] and gaps == [True]
        with pytest.raises(OSError, match="Another Buddy"):
            capture.WindowsInput(b"id", received.append, lambda: None)
    finally:
        mic.close()
    mic.close()
    callback(b"\x00\x10\x00\x20", 1, None, True)
    assert received == [b"\x00\x18"] and gaps == [True]
    assert calls[-2:] == ["abort", "close"]
    assert not mic.active
    with pytest.raises(OSError, match="closed"):
        mic.start()
    replacement = capture.WindowsInput(b"id", received.append, lambda: None)
    replacement.close()


def test_open_failure_releases_capture_lease(sdk):
    fake, _ = sdk
    constructor = fake.RawInputStream

    def fail(**kwargs):
        raise OSError("Device busy")

    fake.RawInputStream = fail
    with pytest.raises(OSError, match="busy"):
        capture.WindowsInput(b"id", lambda _: None, lambda: None)
    fake.RawInputStream = constructor
    mic = capture.WindowsInput(b"id", lambda _: None, lambda: None)
    mic.close()
