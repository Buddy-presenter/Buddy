"""Ephemeral, bounded speech context using the local capture clock.

Consumes only validated live SpeechController results, never journal replay.
"""

import time
from dataclasses import dataclass, field
from typing import cast


@dataclass(frozen=True)
class ContextSegment:
    segment_id: str
    sequence: int
    text: str = field(repr=False)
    final: bool
    start_seconds: float
    end_seconds: float
    capture_end_ns: int
    received_ns: int


@dataclass(frozen=True)
class ContextSnapshot:
    session_id: str
    state_version: int
    epoch: int
    stream_id: str | None
    language: str
    segments: tuple[ContextSegment, ...] = field(repr=False)
    observed_ns: int
    fresh: bool

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments)

    @property
    def candidate_segments(self) -> tuple[ContextSegment, ...]:
        """A bounded, contiguous completed thought; never bridge a partial segment."""
        selected: list[ContextSegment] = []
        size = 0
        for segment in reversed(self.segments):
            if not segment.final or len(selected) == 4:
                break
            extra = len(segment.text) + bool(selected)
            if size + extra > 4096:
                break
            selected.append(segment)
            size += extra
        return tuple(reversed(selected))


class TranscriptContext:
    """One presenter, one live stream; no additional transcript persistence."""

    def __init__(self) -> None:
        self._epoch = 0
        self._boundary_ns = time.monotonic_ns()
        self._session = ""
        self._version = 0
        self._language = ""
        self._stream: str | None = None
        self._sequence = 0
        self._segments: dict[str, ContextSegment] = {}
        self._last_capture_end_ns: int | None = None

    def invalidate(self) -> None:
        self._epoch += 1
        self._boundary_ns = time.monotonic_ns()
        self._segments.clear()
        self._stream = None
        self._sequence = 0
        self._last_capture_end_ns = None

    def bind(self, session: str, version: int, language: str) -> None:
        if (session, version, language) != (self._session, self._version, self._language):
            self.invalidate()
            self._session, self._version, self._language = session, version, language

    def accept(self, event: dict[str, object]) -> None:
        # SpeechController has already strictly validated the wire contract.
        now = time.monotonic_ns()
        if event["session_id"] != self._session:
            return
        stamp = cast(int, event["capture_end_ns"])
        start = cast(float, event["start_seconds"])
        end = cast(float, event["end_seconds"])
        # Discard segments that may straddle a manual change or reconnect.
        capture_start = stamp - round((end - start) * 1_000_000_000)
        if (
            capture_start <= self._boundary_ns
            or not 0 <= now - stamp <= 30_000_000_000
            or now - capture_start > 30_000_000_000
        ):
            return
        stream = cast(str, event["stream_id"])
        sequence = cast(int, event["sequence"])
        if self._stream is not None and self._stream != stream:
            self.invalidate()
            return
        if sequence <= self._sequence:
            return
        key = cast(str, event["segment_id"])
        previous = self._segments.get(key)
        if previous is not None and previous.final:
            self._sequence = sequence
            return
        if (
            self._last_capture_end_ns is not None
            and capture_start - self._last_capture_end_ns > 5_000_000_000
        ):
            boundary = self._last_capture_end_ns + 5_000_000_000
            self.invalidate()
            # Use the capture boundary, not event-arrival time: delayed delivery
            # must not discard the genuinely new utterance after the pause.
            self._boundary_ns = boundary
        self._stream, self._sequence = stream, sequence
        self._expire(now)
        self._segments[key] = ContextSegment(
            key,
            sequence,
            cast(str, event["text"]),
            cast(bool, event["final"]),
            start,
            end,
            stamp,
            now,
        )
        self._last_capture_end_ns = max(self._last_capture_end_ns or stamp, stamp)
        while (
            len(self._segments) > 64 or sum(len(s.text) + 1 for s in self._segments.values()) > 8192
        ):
            del self._segments[next(iter(self._segments))]

    def _expire(self, now: int) -> None:
        self._segments = {
            key: value
            for key, value in self._segments.items()
            if 0 <= now - value.capture_end_ns <= 30_000_000_000
            and now
            - (
                value.capture_end_ns
                - round((value.end_seconds - value.start_seconds) * 1_000_000_000)
            )
            <= 30_000_000_000
        }

    def snapshot(self) -> ContextSnapshot:
        now = time.monotonic_ns()
        self._expire(now)
        segments = tuple(sorted(self._segments.values(), key=lambda s: s.start_seconds))
        fresh = bool(segments and now - segments[-1].capture_end_ns <= 5_000_000_000)
        return ContextSnapshot(
            self._session,
            self._version,
            self._epoch,
            self._stream,
            self._language,
            segments,
            now,
            fresh,
        )
