"""Nonblocking Qt speech connection. No provider credentials live on the desktop."""

import json
import math
import time
from collections.abc import Callable
from uuid import UUID

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

from buddy_desktop.config import AppConfig

ERRORS = {
    "speech_unconfigured": "Speech recognition is not configured on this server.",
    "provider_configuration": "The server's speech provider credentials need attention.",
    "provider_unavailable": "The speech provider is temporarily unavailable.",
    "speech_unavailable": "The speech service encountered an internal error.",
    "connection_lost": "The speech service connection was lost.",
    "event_journal_unavailable": "Speech synchronization is temporarily unavailable.",
    "invalid_configuration": "The speech service rejected its session configuration.",
    "session_503": "The presentation session service is temporarily unavailable.",
    "unsupported_language": "This recognition language is not supported.",
    "unsupported_configuration": "The provider does not support this audio or language setting.",
    "provider_rate_limited": "The speech provider is busy.",
    "stream_busy": (
        "The speech service is at its active-stream limit or server capacity is reached."
    ),
    "session_401": "Sign in again to use speech recognition.",
    "session_403": "Speech access to this presentation was denied.",
    "session_404": "This presentation is no longer available.",
    "session_409": "This presentation session has ended.",
}


class SpeechController(QObject):
    status_changed = Signal(str)
    transcript_received = Signal(object)
    cleared = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.socket: QWebSocket | None = None
        self._active = False
        self._ready = False
        self._generation = 0
        self._attempts = 0
        self._access = ""
        self._url = ""
        self._session = ""
        self._sample_rate = 0
        self._language = "en-US"
        self._stream = ""
        self._sequence = 0
        self._final_end = 0.0
        self._final_start = -1.0
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(lambda: self._fail("Speech connection timed out.", True))
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._connect)

    def start(
        self, config: AppConfig, access: str, session: str, sample_rate: int, language: str
    ) -> None:
        self.stop()
        UUID(session)
        self._url = (
            "wss" + config.api_url[5:]
            if config.api_url.startswith("https:")
            else "ws" + config.api_url[4:]
        ) + f"/api/v1/sessions/{session}/speech"
        self._access, self._session = access, session
        self._sample_rate, self._language = sample_rate, language
        self._active = True
        self._attempts = 0
        self._connect()

    def _connect(self) -> None:
        if not self._active:
            return
        self._generation += 1
        generation = self._generation
        socket = QWebSocket(parent=self)
        self.socket = socket
        socket.setMaxAllowedIncomingMessageSize(65536)
        socket.setMaxAllowedIncomingFrameSize(65536)
        socket.setReadBufferSize(65536)

        def current(callback: Callable[[], None]) -> None:
            if self._active and generation == self._generation:
                callback()

        def connected() -> None:
            socket.sendTextMessage(
                json.dumps(
                    {"version": 1, "language": self._language, "sample_rate": self._sample_rate}
                )
            )

        socket.connected.connect(lambda: current(connected))
        socket.textMessageReceived.connect(lambda message: current(lambda: self._message(message)))
        socket.disconnected.connect(
            lambda: current(lambda: self._fail("Speech connection lost.", True))
        )
        socket.errorOccurred.connect(
            lambda _error: current(lambda: self._fail("Speech connection failed.", True))
        )
        request = QNetworkRequest(QUrl(self._url))
        request.setRawHeader(
            QByteArray(b"Authorization"), QByteArray(("Bearer " + self._access).encode())
        )
        self.status_changed.emit("Connecting speech recognition…")
        self._deadline.start(20000)
        socket.open(request)

    def _message(self, raw: str) -> None:
        try:
            if len(raw) > 65536:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            if data.get("type") == "error":
                code = str(data.get("code", ""))
                message = ERRORS.get(code, "Speech recognition is temporarily unavailable.")
                diagnostic_id = data.get("diagnostic_id")
                if isinstance(diagnostic_id, str):
                    try:
                        diagnostic_id = str(UUID(diagnostic_id))
                    except ValueError:
                        diagnostic_id = ""
                    if diagnostic_id:
                        message += f" Reference: {diagnostic_id}."
                self._fail(
                    message,
                    data.get("retryable") is True,
                )
                return
            if data.get("type") == "ready":
                if self._ready or data.get("version") != 1:
                    raise ValueError
                self._stream = str(UUID(data["stream_id"]))
                self._sequence, self._final_end = 0, 0.0
                self._final_start = -1.0
                self._ready = True
                self._deadline.stop()
                self.status_changed.emit(
                    "Transcribing • audio sent to Deepgram (EU); "
                    "transcript available for session sync for five minutes"
                )
                return
            if not self._ready or data.get("type") != "transcript" or data.get("version") != 1:
                raise ValueError
            if data.get("session_id") != self._session or data.get("stream_id") != self._stream:
                raise ValueError
            sequence = data["sequence"]
            if type(sequence) is not int or sequence != self._sequence + 1:
                raise ValueError
            start, end = data["start_seconds"], data["end_seconds"]
            if any(type(v) not in {int, float} or not math.isfinite(v) for v in (start, end)):
                raise ValueError
            # Adjacent provider boundaries can differ by float32 rounding (e.g.
            # 4.5500003 vs 4.55). Permit that precision only; finalized segment
            # starts remain strictly increasing so duplicates cannot be replayed.
            adjacent = start >= self._final_end or math.isclose(
                start, self._final_end, rel_tol=2**-23, abs_tol=0.000001
            )
            if (
                not 0 <= start <= end <= 1801
                or not adjacent
                or start <= self._final_start
                or type(data["final"]) is not bool
            ):
                raise ValueError
            if data.get("segment_id") != f"{self._stream}:{round(start * 1_000_000)}":
                raise ValueError
            text, stamp = data["text"], data["capture_end_ns"]
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise ValueError
            now = time.monotonic_ns()
            if type(stamp) is not int or not 0 < stamp <= now or now - stamp > 30_000_000_000:
                raise ValueError
            self._sequence = sequence
            if data["final"]:
                self._final_end = end
                self._final_start = start
            data["latency_ms"] = round((now - stamp) / 1_000_000)
            self.transcript_received.emit(data)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._fail("Speech returned an invalid or stale update. Restart recognition.", False)

    def send(self, packet: bytes) -> None:
        if not self._active or not self._ready or self.socket is None:
            return  # Never replay audio captured during connection/reconnection gaps.
        if self.socket.bytesToWrite() > 32768:
            self._fail("Speech connection is too slow; audio was discarded.", True)
            return
        self.socket.sendBinaryMessage(QByteArray(packet))

    def audio_gap(self) -> None:
        if self._active and self._ready:
            self._fail("An audio gap occurred; restarting recognition.", True)

    def _detach(self) -> None:
        self._generation += 1
        self._ready = False
        self._deadline.stop()
        socket, self.socket = self.socket, None
        if socket is not None:
            socket.abort()
            socket.deleteLater()
        self.cleared.emit()

    def _fail(self, message: str, retryable: bool) -> None:
        if not self._active:
            return
        self._detach()
        if retryable and self._attempts < 3:
            self._attempts += 1
            self.status_changed.emit(message + " Reconnecting; speech during the gap is discarded.")
            self._retry.start(1000 * 2 ** (self._attempts - 1))
        else:
            self._active = False
            self._access = ""
            self.status_changed.emit(message + " Stop and restart the microphone to retry.")

    def stop(self) -> None:
        self._active = False
        self._retry.stop()
        self._detach()
        self._access = ""
