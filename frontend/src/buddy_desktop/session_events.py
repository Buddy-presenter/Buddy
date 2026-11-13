"""Bounded SSE client with explicit GUI acknowledgment and reconnect cursors."""

import http.client
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Thread
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from PySide6.QtCore import QObject, Signal

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.presentation_session_service import PresentationSession, parse_snapshot
from buddy_desktop.services import NoRedirects, ServiceError

MAX_FRAME = 32768
RETRY_DELAYS = (1, 2, 4, 8, 8, 8, 8, 8)


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    sequence: int
    state_version: int
    payload: dict[str, object]
    snapshot: PresentationSession | None = None


def parse_event(raw: object, session_id: str) -> SessionEvent:
    if not isinstance(raw, dict) or set(raw) != {
        "protocol",
        "session_id",
        "sequence",
        "state_version",
        "created_at",
        "payload",
    }:
        raise ValueError
    if raw["protocol"] != "session-events.v1" or raw["session_id"] != session_id:
        raise ValueError
    UUID(session_id)
    for key in ("sequence", "state_version"):
        if type(raw[key]) is not int or not 0 <= raw[key] < 2**63:
            raise ValueError
    if not isinstance(raw["created_at"], str):
        raise ValueError
    if datetime.fromisoformat(raw["created_at"]).tzinfo is None:
        raise ValueError
    payload = raw["payload"]
    if not isinstance(payload, dict):
        raise ValueError
    snapshot = None
    kind = payload.get("kind")
    if kind == "player.state":
        if set(payload) != {"kind", "snapshot"}:
            raise ValueError
        snapshot = parse_snapshot({"data": payload["snapshot"], "request_id": "session-events"})
        if snapshot.id != session_id or snapshot.state_version != raw["state_version"]:
            raise ValueError
    elif kind == "speech.transcript":
        if set(payload) != {
            "kind",
            "stream_id",
            "segment_id",
            "revision",
            "text",
            "final",
            "language",
            "start_ms",
            "end_ms",
        }:
            raise ValueError
        UUID(payload["stream_id"])
        if (
            not isinstance(payload["segment_id"], str)
            or not 1 <= len(payload["segment_id"]) <= 128
            or any(character.isspace() for character in payload["segment_id"])
            or not isinstance(payload["text"], str)
            or not 1 <= len(payload["text"]) <= 4096
            or type(payload["final"]) is not bool
            or not isinstance(payload["language"], str)
            or not 2 <= len(payload["language"]) <= 32
        ):
            raise ValueError
        for key, bound in (("revision", 1000000), ("start_ms", 86400000), ("end_ms", 86400000)):
            if type(payload[key]) is not int or not 0 <= payload[key] <= bound:
                raise ValueError
        if payload["end_ms"] < payload["start_ms"]:
            raise ValueError
    elif kind == "decision.status":
        if set(payload) != {"kind", "decision_id", "outcome", "action", "reason"}:
            raise ValueError
        UUID(payload["decision_id"])
        if (
            payload["outcome"] not in {"applied", "abstained", "rejected"}
            or payload["action"] not in {None, "next_slide", "previous_slide", "go_to_slide"}
            or payload["reason"]
            not in {"accepted", "uncertain", "stale", "unsupported", "manual_override"}
        ):
            raise ValueError
    else:
        raise ValueError
    return SessionEvent(session_id, raw["sequence"], raw["state_version"], payload, snapshot)


def read_frames(stream: BinaryIO, stopped: Event) -> Iterator[list[str]]:
    """Read bounded frames without buffering a response or an unbounded line."""
    frame: list[str] = []
    size = 0
    while not stopped.is_set():
        line = stream.readline(MAX_FRAME + 1)
        if not line:
            return
        size += len(line)
        if size > MAX_FRAME:
            raise ValueError
        if line in (b"\n", b"\r\n"):
            if frame:
                yield frame
            frame, size = [], 0
        else:
            frame.append(line.decode("utf-8").rstrip("\r\n"))


class SessionEventStream(QObject):
    received = Signal(int, object)
    failed = Signal(int, str, bool)
    connection_changed = Signal(int, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._stopped = Event()
        self._ack = Event()
        self._thread: Thread | None = None
        self._generation = 0
        self._key: tuple[str, str, str] | None = None
        if parent is not None:
            parent.destroyed.connect(self.stop)

    @property
    def generation(self) -> int:
        return self._generation

    def bind(
        self, config: AppConfig, access: str | None, session: PresentationSession | None
    ) -> None:
        key = (config.api_url, access, session.id) if access and session else None
        if key == self._key:
            return
        self.stop()
        self._key = key
        if key is None:
            return
        self._stopped, self._ack = Event(), Event()
        self.connection_changed.emit(self._generation, "connecting")
        self._thread = Thread(
            target=self._run,
            args=(config, key, self._generation, self._stopped, self._ack),
            daemon=True,
        )
        self._thread.start()

    def acknowledge(self, generation: int | None = None) -> None:
        if generation is None or generation == self._generation:
            self._ack.set()

    def stop(self) -> None:
        self._generation += 1
        self._stopped.set()
        self._ack.set()
        self._key = None

    def _run(
        self,
        config: AppConfig,
        key: tuple[str, str, str],
        generation: int,
        stopped: Event,
        ack: Event,
    ) -> None:
        _, access, identifier = key
        cursor: int | None = None
        state_version = -1
        failures = 0
        while not stopped.is_set():
            headers = {"Authorization": "Bearer " + access, "Accept": "text/event-stream"}
            if cursor is not None:
                headers["Last-Event-ID"] = str(cursor)
            try:
                request = Request(
                    config.api_url + f"/api/v1/sessions/{UUID(identifier)}/sync/events",
                    headers=headers,
                )
                with build_opener(NoRedirects()).open(
                    request, timeout=min(config.request_timeout, 2)
                ) as response:
                    if response.headers.get_content_type() != "text/event-stream":
                        raise ValueError
                    if not stopped.is_set():
                        self.connection_changed.emit(generation, "connected")
                    for frame in read_frames(cast(BinaryIO, response), stopped):
                        event_type = next((s[7:] for s in frame if s.startswith("event: ")), "")
                        if event_type == "access_closed":
                            raise LibraryAccessExpired(
                                "Session access ended. Sign in and sync again."
                            )
                        if event_type == "unavailable":
                            raise ServiceError("Session updates are reconnecting.")
                        if event_type != "session_event":
                            if all(line.startswith(":") for line in frame):
                                failures = 0
                            continue
                        data = [s[6:] for s in frame if s.startswith("data: ")]
                        ids = [s[4:] for s in frame if s.startswith("id: ")]
                        if len(data) != 1 or len(ids) != 1:
                            raise ValueError
                        event = parse_event(json.loads(data[0]), identifier)
                        if ids[0] != str(event.sequence):
                            raise ValueError
                        if cursor is not None and event.sequence <= cursor:
                            continue
                        # State observations can supersede expired history. A speech
                        # or decision notification never carries executable tools.
                        if event.state_version < state_version:
                            raise ValueError
                        if (
                            cursor is not None
                            and event.sequence != cursor + 1
                            and not event.snapshot
                        ):
                            raise ValueError
                        ack.clear()
                        if stopped.is_set():
                            return
                        self.received.emit(generation, event)
                        # One unacknowledged frame, independent of GUI speed.
                        while not stopped.is_set() and not ack.wait(0.1):
                            pass
                        if stopped.is_set():
                            return
                        cursor, state_version = event.sequence, event.state_version
                        failures = 0
                        if event.snapshot and event.snapshot.status == "ended":
                            return
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if status in {401, 403, 404}:
                    if not stopped.is_set():
                        self.failed.emit(
                            generation, "Session access ended. Sign in and sync again.", True
                        )
                    return
                if status == 409:
                    cursor = None
            except LibraryAccessExpired as exc:
                if not stopped.is_set():
                    self.failed.emit(generation, str(exc), True)
                return
            except (ValueError, TypeError, KeyError, AttributeError):
                if not stopped.is_set():
                    self.failed.emit(
                        generation, "Session updates returned an invalid response.", False
                    )
                return
            except (ServiceError, URLError, OSError, http.client.HTTPException):
                pass
            failures += 1
            if failures > len(RETRY_DELAYS):
                if not stopped.is_set():
                    self.failed.emit(
                        generation, "Session updates disconnected. Retry session sync.", False
                    )
                return
            if not stopped.is_set():
                self.connection_changed.emit(generation, "reconnecting")
            stopped.wait(RETRY_DELAYS[failures - 1])
