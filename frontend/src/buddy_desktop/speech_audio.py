"""Streaming native-format downmix to mono PCM16, without resampling or files."""

import math
import struct


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
