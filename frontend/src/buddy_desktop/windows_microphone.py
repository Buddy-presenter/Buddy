"""Explicit WASAPI-exclusive input, bound to the exact selected Windows endpoint."""

from __future__ import annotations

import ctypes
import importlib
import math
import sys
from collections.abc import Callable
from threading import Event, Lock
from typing import Protocol, cast

from buddy_desktop.speech_audio import PCM16Encoder

_capture_lease = Lock()


class InputStream(Protocol):
    @property
    def active(self) -> bool: ...
    def start(self) -> None: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


class EndpointLookup:
    """Bridge the public PortAudio WASAPI extension to IMMDevice::GetId.

    sounddevice 0.5.6 does not expose this extension through CFFI. Load the same
    bundled DLL it already uses; never resolve devices through display-name matching.
    """

    def __init__(self, library_path: str):
        if sys.platform != "win32":
            raise OSError("Windows endpoint discovery is unavailable on this platform.")
        self.library = ctypes.CDLL(library_path)
        self.lookup = self.library.PaWasapi_GetIMMDevice
        self.lookup.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        self.lookup.restype = ctypes.c_int
        self.ole = ctypes.OleDLL("ole32")
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None

    def __call__(self, index: int) -> str:
        if sys.platform != "win32":
            raise OSError("Windows endpoint discovery is unavailable on this platform.")
        device = ctypes.c_void_p()
        if self.lookup(index, ctypes.byref(device)) != 0 or not device.value:
            raise OSError("Microphone endpoint lookup failed.")
        # PortAudio owns IMMDevice: do not release the borrowed interface.
        table = ctypes.cast(device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_id = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(table[5])
        identifier = ctypes.c_void_p()
        try:
            if get_id(device, ctypes.byref(identifier)) < 0 or not identifier.value:
                raise OSError("Microphone endpoint identity is unavailable.")
            return ctypes.wstring_at(identifier)
        finally:
            if identifier.value:
                self.ole.CoTaskMemFree(identifier)


def select_input(
    device_id: bytes,
    devices: list[dict[str, object]],
    apis: list[dict[str, object]],
    endpoint: Callable[[int], str],
) -> tuple[int, int, int]:
    expected = device_id.decode("utf-8").casefold()
    matches: list[tuple[int, int, int]] = []
    for index, device in enumerate(devices):
        channels, api = device.get("max_input_channels"), device.get("hostapi")
        if type(channels) is not int or channels < 1 or type(api) is not int:
            continue
        if not 0 <= api < len(apis) or apis[api].get("name") != "Windows WASAPI":
            continue
        if endpoint(index).casefold() != expected:
            continue
        rate = device.get("default_samplerate")
        if (
            type(rate) not in {int, float}
            or not math.isfinite(cast(float, rate))
            or not 8000 <= cast(float, rate) <= 96000
            or int(cast(float, rate)) != rate
            or channels > 32
        ):
            raise ValueError("Unsupported microphone format.")
        matches.append((index, int(cast(float, rate)), channels))
    if len(matches) != 1:
        raise ValueError("The selected microphone is unavailable or ambiguous.")
    return matches[0]


class WindowsInput:
    def __init__(
        self,
        device_id: bytes,
        consume: Callable[[bytes], None],
        gap: Callable[[], None],
    ):
        if not _capture_lease.acquire(blocking=False):
            raise OSError("Another Buddy microphone is active.")
        self.closed = Event()
        self.stream: InputStream | None = None
        self._leased = True
        try:
            sdk = importlib.import_module("sounddevice")
            # The pinned SDK's lifecycle wrappers keep its initialization count
            # balanced. Reinitialize only with our exclusive process lease held:
            # desktop WASAPI has no supported live device-list refresh extension.
            sdk._terminate()
            sdk._initialize()
            endpoint = EndpointLookup(sdk._libname)
            index, self.sample_rate, channels = select_input(
                device_id, list(sdk.query_devices()), list(sdk.query_hostapis()), endpoint
            )
            encoder = PCM16Encoder("Int16", channels)

            def capture(data: object, _frames: int, _timing: object, status: object) -> None:
                if self.closed.is_set():
                    return
                if status:
                    gap()
                # PortAudio supplies a plain buffer; never retain its native memory.
                pcm = encoder.encode(bytes(cast(bytes, data)))
                if pcm and not self.closed.is_set():
                    consume(pcm)

            self.stream = cast(
                InputStream,
                sdk.RawInputStream(
                    device=index,
                    samplerate=self.sample_rate,
                    channels=channels,
                    dtype="int16",
                    blocksize=min(1600, self.sample_rate // 20),
                    extra_settings=sdk.WasapiSettings(exclusive=True),
                    callback=capture,
                ),
            )
        except Exception:
            self.close()
            raise

    @property
    def active(self) -> bool:
        return self.stream is not None and bool(self.stream.active)

    def start(self) -> None:
        if self.stream is None or self.closed.is_set():
            raise OSError("Microphone stream is closed.")
        self.stream.start()

    def close(self) -> None:
        self.closed.set()
        stream, self.stream = self.stream, None
        try:
            if stream is not None:
                try:
                    stream.abort()
                finally:
                    stream.close()
        finally:
            if self._leased:
                self._leased = False
                _capture_lease.release()
