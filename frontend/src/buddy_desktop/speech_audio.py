"""Streaming native-format downmix and speech-rate conversion, without audio files."""

import importlib
import math
import struct
from types import ModuleType
from typing import Any

SPEECH_SAMPLE_RATE = 16000


class PCM16Resampler:
    """Bounded, anti-aliased streaming conversion; discarded on stop, never flushed late."""

    def __init__(self, native_rate: int):
        if not 8000 <= native_rate <= 96000:
            raise ValueError("Unsupported microphone sample rate.")
        self.numpy: ModuleType | None = None
        self.stream: Any = None
        if native_rate != SPEECH_SAMPLE_RATE:
            # Created on the capture worker, keeping native library startup off
            # the GUI thread. Native 16 kHz packets need no conversion at all.
            self.numpy = importlib.import_module("numpy")
            soxr = importlib.import_module("soxr")
            self.stream = soxr.ResampleStream(
                native_rate, SPEECH_SAMPLE_RATE, 1, dtype="int16", quality="HQ"
            )

    def encode(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("Incomplete PCM16 sample.")
        if self.stream is None or self.numpy is None:
            return pcm
        samples = self.numpy.frombuffer(pcm, dtype="<i2")
        return bytes(self.stream.resample_chunk(samples).tobytes())

    @property
    def delay_ns(self) -> int:
        if self.stream is None:
            return 0
        return round(float(self.stream.delay()) * 1_000_000_000 / SPEECH_SAMPLE_RATE)


class PCM16Encoder:
    def __init__(self, sample_format: str, channels: int):
        formats = {"UInt8": ("B", 1), "Int16": ("h", 2), "Int32": ("i", 4), "Float": ("f", 4)}
        if sample_format not in formats or not 1 <= channels <= 32:
            raise ValueError("Unsupported microphone format.")
        self.code, self.size = formats[sample_format]
        self.channels = channels
        self.pending = b""

    def encode(self, data: bytes) -> bytes:
        data = self.pending + data
        frame_size = self.size * self.channels
        usable = len(data) - len(data) % frame_size
        self.pending = data[usable:]
        output = bytearray()
        for frame in struct.iter_unpack("<" + self.code * self.channels, data[:usable]):
            value = sum(frame) / self.channels
            if self.code == "B":
                value = (value - 128) * 256
            elif self.code == "i":
                value /= 65536
            elif self.code == "f":
                value *= 32768
            if not math.isfinite(value):
                value = 0
            output.extend(struct.pack("<h", max(-32768, min(32767, round(value)))))
        return bytes(output)
