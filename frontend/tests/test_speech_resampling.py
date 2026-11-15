"""Speech bandwidth, timing, anti-aliasing and reconnect-boundary regressions."""

import struct
import time
from queue import Queue
from threading import Event, Lock

import numpy as np
import pytest

from buddy_desktop.microphone import _CaptureWorker
from buddy_desktop.speech_audio import PCM16Resampler


def tone(rate, frequency):
    return (12000 * np.sin(2 * np.pi * frequency * np.arange(rate) / rate)).astype("<i2")


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 96000])
def test_stream_preserves_speech_tone_and_duration_across_chunk_boundaries(rate):
    stream = PCM16Resampler(rate)
    source = tone(rate, 1000).tobytes()
    output = b"".join(stream.encode(source[i : i + 634]) for i in range(0, len(source), 634))
    samples = np.frombuffer(output, dtype="<i2")
    assert abs(len(samples) + stream.delay_ns * 16000 / 1e9 - 16000) < 2
    middle = samples[3200:11200].astype(float)
    peak = np.argmax(abs(np.fft.rfft(middle))) * 16000 / len(middle)
    assert abs(peak - 1000) < 5
    assert 8200 < np.sqrt(np.mean(middle**2)) < 8700


def test_high_frequency_is_filtered_instead_of_aliasing_into_speech():
    source = tone(48000, 10000).tobytes()
    stream = PCM16Resampler(48000)
    output = stream.encode(source)
    middle = np.frombuffer(output, dtype="<i2")[3200:11200].astype(float)
    assert np.sqrt(np.mean(middle**2)) < 100


def test_native_16k_is_exact_and_incomplete_samples_are_rejected():
    stream = PCM16Resampler(16000)
    pcm = struct.pack("<hhh", -32768, 0, 32767)
    assert stream.encode(pcm) == pcm and stream.delay_ns == 0
    with pytest.raises(ValueError, match="Incomplete"):
        stream.encode(b"x")


def test_capture_packetization_accounts_for_filter_delay_and_never_flushes_on_stop(qapp):
    frames = Queue(maxsize=64)
    accepting = Event()
    accepting.set()
    worker = _CaptureWorker(frames, accepting, Lock())
    worker.generation = 1
    worker.resampler = PCM16Resampler(48000)
    source = tone(48000, 1000).tobytes()
    for i in range(0, len(source), 3200):
        worker._queue_pcm(source[i : i + 3200], 1)
    packets = []
    while not frames.empty():
        packets.append(frames.get_nowait())
    assert packets and all(10 <= len(packet) <= 1608 for packet in packets)
    stamps = [struct.unpack(">Q", packet[:8])[0] for packet in packets]
    assert all(a < b for a, b in zip(stamps, stamps[1:], strict=False))
    assert stamps[-1] <= time.monotonic_ns()
    # Only 16 kHz PCM is queued even though the device supplied 48 kHz.
    assert 30000 < sum(len(p) - 8 for p in packets) <= 32000
    worker.stop()
    assert frames.empty() and worker.resampler is None
    worker._queue_pcm(b"\0\0", 0)
    assert frames.empty()
