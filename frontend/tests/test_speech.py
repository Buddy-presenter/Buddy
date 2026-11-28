"""PCM conversion, transcript freshness, retry limits, and presenter cleanup."""

import json
import struct
import time
from uuid import uuid4

import pytest

from buddy_desktop.speech import SpeechController
from buddy_desktop.speech_audio import PCM16Encoder


@pytest.mark.parametrize(
    "kind,channels,raw,expected",
    [
        ("UInt8", 1, bytes([0, 128, 255]), [-32768, 0, 32512]),
        ("Float", 2, struct.pack("<ffff", 1, -1, 0.5, 0.5), [0, 16384]),
        ("Int16", 2, struct.pack("<hhhh", -1000, 1000, 1000, 3000), [0, 2000]),
        ("Int32", 1, struct.pack("<ii", -2147483648, 2147483647), [-32768, 32767]),
    ],
)
def test_native_formats_and_chunk_boundaries(kind, channels, raw, expected):
    encoder = PCM16Encoder(kind, channels)
    # Native reads may split samples or channel frames.
    output = b"".join(encoder.encode(bytes([item])) for item in raw)
    assert list(struct.unpack("<" + "h" * len(expected), output)) == expected


def test_nonfinite_audio_is_silence():
    encoder = PCM16Encoder("Float", 1)
    assert encoder.encode(struct.pack("<ff", float("nan"), float("inf"))) == b"\0" * 4


@pytest.fixture
def controller(qapp):
    control = SpeechController()
    control._active = True
    control._session = str(uuid4())
    stream = str(uuid4())
    control._message(json.dumps({"type": "ready", "version": 1, "stream_id": stream}))
    try:
        yield control
    finally:
        control.stop()


def event(control, **changes):
    data = {
        "type": "transcript",
        "version": 1,
        "session_id": control._session,
        "stream_id": control._stream,
        "sequence": 1,
        "segment_id": control._stream + ":0",
        "final": True,
        "text": "Next slide",
        "start_seconds": 0,
        "end_seconds": 0.1,
        "capture_end_ns": time.monotonic_ns() - 10000000,
    }
    return dict(data, **changes)


def test_valid_result_emits_measured_latency_and_stop_clears(controller):
    updates, cleared = [], []
    controller.transcript_received.connect(updates.append)
    controller.cleared.connect(lambda: cleared.append(True))
    controller._message(json.dumps(event(controller)))
    assert updates[0]["text"] == "Next slide" and updates[0]["latency_ms"] >= 10
    controller.stop()
    assert cleared and not controller._active and not controller._access


def test_valid_but_delayed_transcript_discards_context_and_reconnects(controller):
    updates, cleared = [], []
    controller.transcript_received.connect(updates.append)
    controller.cleared.connect(lambda: cleared.append(True))
    controller._message(
        json.dumps(event(controller, capture_end_ns=time.monotonic_ns() - 6_000_000_000))
    )
    assert not updates and cleared
    assert controller._active and not controller._ready and controller._retry.isActive()
    assert controller._attempts == 1


def test_filter_delayed_packets_from_connection_gap_are_not_replayed(controller):
    sent = []

    class Socket:
        def bytesToWrite(self):
            return 0

        def sendBinaryMessage(self, packet):
            sent.append(bytes(packet))

        def abort(self):
            pass

        def deleteLater(self):
            pass

    controller.socket = Socket()
    controller._sample_rate = 16000
    boundary = controller._audio_boundary_ns
    # 50 ms of audio: before ready, straddling ready, then wholly after ready.
    for end in (boundary - 1, boundary + 25_000_000, boundary + 100_000_000):
        controller.send(struct.pack(">Q", end) + b"\0" * 1600)
    assert len(sent) == 1
    assert struct.unpack(">Q", sent[0][:8])[0] == boundary + 100_000_000


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": str(uuid4())},
        {"stream_id": str(uuid4())},
        {"sequence": 2},
        {"capture_end_ns": 1},
        {"end_seconds": float("nan")},
        {"final": "true"},
        {"text": "x" * 4097},
        {"segment_id": "wrong"},
    ],
)
def test_stale_or_invalid_result_never_emits(controller, change):
    updates = []
    controller.transcript_received.connect(updates.append)
    controller._message(json.dumps(event(controller, **change)))
    assert not updates and not controller._active


def test_duplicate_final_never_emits_twice(controller):
    updates = []
    controller.transcript_received.connect(updates.append)
    data = event(controller)
    controller._message(json.dumps(data))
    controller._message(json.dumps(dict(data, sequence=2)))
    assert len(updates) == 1 and not controller._active


@pytest.mark.parametrize("boundary,rounded", [(4.5500003, 4.55), (1790.0001, 1790.0)])
def test_adjacent_float32_provider_boundaries_do_not_end_recognition(controller, boundary, rounded):
    updates = []
    controller.transcript_received.connect(updates.append)
    controller._message(json.dumps(event(controller, end_seconds=boundary)))
    controller._message(
        json.dumps(
            event(
                controller,
                sequence=2,
                start_seconds=rounded,
                end_seconds=rounded + 1,
                segment_id=f"{controller._stream}:{round(rounded * 1_000_000)}",
            )
        )
    )
    assert len(updates) == 2 and controller._active


def test_actual_overlap_remains_invalid(controller):
    controller._message(json.dumps(event(controller, end_seconds=4.55)))
    controller._message(
        json.dumps(
            event(
                controller,
                sequence=2,
                start_seconds=4.54,
                end_seconds=5,
                segment_id=f"{controller._stream}:4540000",
            )
        )
    )
    assert not controller._active


def test_duplicate_zero_duration_final_is_rejected(controller):
    controller._message(json.dumps(event(controller, end_seconds=0)))
    controller._message(json.dumps(event(controller, sequence=2, end_seconds=0)))
    assert not controller._active


def test_retry_budget_and_stop_cancel_reconnect(controller):
    for _ in range(3):
        controller._fail("Temporary failure.", True)
        assert controller._retry.isActive()
        controller._retry.stop()
    controller._fail("Temporary failure.", True)
    assert not controller._active and not controller._retry.isActive()
    controller.stop()


def test_nonretryable_configuration_error_does_not_loop(controller):
    controller._message(
        json.dumps({"type": "error", "code": "speech_unconfigured", "retryable": False})
    )
    assert not controller._active and not controller._retry.isActive()


def test_server_error_shows_safe_diagnostic_reference(controller):
    diagnostic_id = str(uuid4())
    updates = []
    controller.status_changed.connect(updates.append)
    controller._message(
        json.dumps(
            {
                "type": "error",
                "code": "provider_unavailable",
                "retryable": False,
                "diagnostic_id": diagnostic_id,
            }
        )
    )
    assert updates[-1] == (
        f"The speech provider is temporarily unavailable. Reference: {diagnostic_id}. "
        "Stop and restart the microphone to retry."
    )


def test_malformed_server_diagnostic_reference_is_omitted(controller):
    updates = []
    controller.status_changed.connect(updates.append)
    controller._message(
        json.dumps(
            {
                "type": "error",
                "code": "speech_unavailable",
                "retryable": False,
                "diagnostic_id": "not-a-uuid",
            }
        )
    )
    assert updates[-1] == (
        "The speech service encountered an internal error. "
        "Stop and restart the microphone to retry."
    )


def test_every_connection_uses_a_current_token(qapp, monkeypatch):
    # #230: speech reconnects can happen long after the microphone started.
    from buddy_desktop import speech
    from buddy_desktop.config import AppConfig

    opened = []
    monkeypatch.setattr(
        speech.QWebSocket,
        "open",
        lambda self, request: opened.append(request.rawHeader("Authorization").data().decode()),
    )
    tokens = iter(["first-token", "refreshed-token"])
    control = SpeechController()
    try:
        control.start(
            AppConfig("http://127.0.0.1:9"), lambda: next(tokens), str(uuid4()), 16000, "en-US"
        )
        control._connect()
        assert opened == ["Bearer first-token", "Bearer refreshed-token"]
    finally:
        control.stop()


def test_expired_sign_in_stops_speech_without_retrying(qapp, monkeypatch):
    from buddy_desktop import speech
    from buddy_desktop.config import AppConfig
    from buddy_desktop.library_service import LibraryAccessExpired

    opened = []
    monkeypatch.setattr(speech.QWebSocket, "open", lambda self, request: opened.append(request))

    def expired() -> str:
        raise LibraryAccessExpired("Buddy sign-in expired. Please sign in again.")

    control = SpeechController()
    statuses = []
    control.status_changed.connect(statuses.append)
    control.start(AppConfig("http://127.0.0.1:9"), expired, str(uuid4()), 16000, "en-US")
    assert opened == []
    assert not control._active and not control._retry.isActive()
    assert "Sign in again" in statuses[-1]
