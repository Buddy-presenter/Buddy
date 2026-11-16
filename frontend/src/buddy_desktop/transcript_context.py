"""Ephemeral, bounded speech context using the local capture clock.

Consumes only validated live SpeechController results, never journal replay.
"""

import re
import time
from dataclasses import dataclass, field
from typing import cast

_CORRECTION_STARTS = (
    "actually",
    "no",
    "wait",
    "instead",
    "i mean",
    "sorry",
    "rather",
    "scratch that",
    "hold on",
    "don't",
    "do not",
    "never",
    "cancel that",
    "change that",
    "forget that",
    "i don't",
    "i do not",
    "en realidad",
    "no espera",
    "non",
    "attends",
    "nein",
    "warte",
    "لا",
    "انتظر",
    "قصدي",
)


def _correction_follows(prefix: str, text: str) -> bool:
    if not text.startswith(prefix):
        return False
    tail = re.sub(r"^[\s,.;:!?—–-]+", "", text[len(prefix) :]).casefold()
    return any(
        re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", tail) is not None
        for marker in _CORRECTION_STARTS
    )


def _starts_correction(text: str) -> bool:
    value = re.sub(r"^[\s,.;:!?—–-]+", "", text).casefold()
    return any(
        value == marker or value.startswith(marker + " ") or value.startswith(marker + ",")
        for marker in _CORRECTION_STARTS
    )


@dataclass(frozen=True)
class ContextSegment:
    segment_id: str
    sequence: int
    text: str = field(repr=False)
    final: bool
    start_seconds: float
    end_seconds: float
    capture_end_ns: int
    stable_text: str = field(default="", repr=False)


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
        """Recent completed speech within the query budget; never bridge a partial."""
        selected: list[ContextSegment] = []
        size = 0
        for segment in reversed(self.segments):
            if not segment.final:
                break
            extra = len(segment.text) + bool(selected)
            if size + extra > 4096:
                break
            selected.append(segment)
            size += extra
        return tuple(reversed(selected))

    @property
    def decision_segments(self) -> tuple[ContextSegment, ...]:
        """Recent final speech plus a stable prefix of the active interim segment."""
        selected: list[ContextSegment] = []
        size = 0
        for segment in reversed(self.segments):
            if not segment.final:
                if selected:
                    break
                if not segment.stable_text:
                    continue
                segment = ContextSegment(
                    segment.segment_id, segment.sequence, segment.stable_text, False,
                    segment.start_seconds, segment.end_seconds, segment.capture_end_ns,
                    segment.stable_text,
                )
            extra = len(segment.text) + bool(selected)
            if size + extra > 4096:
                break
            selected.append(segment)
            size += extra
        return tuple(reversed(selected))

    def matches(self, current: "ContextSnapshot") -> bool:
        """A result may be used only for the same fresh, unchanged live input."""
        return (
            self.fresh
            and current.fresh
            and bool(self.candidate_segments)
            and self.session_id == current.session_id
            and self.state_version == current.state_version
            and self.epoch == current.epoch
            and self.stream_id == current.stream_id
            and self.language == current.language
            and self.segments == current.segments
        )

    def matches_decision(self, current: "ContextSnapshot") -> bool:
        """Accept ordinary continuation while rejecting corrected decision evidence."""
        submitted = self.decision_segments
        if not (
            self.fresh and current.fresh and submitted
            and self.session_id == current.session_id
            and self.state_version == current.state_version
            and self.epoch == current.epoch
            and self.stream_id == current.stream_id
            and self.language == current.language
        ):
            return False
        now_by_id = {segment.segment_id: segment for segment in current.segments}
        submitted_ids = {segment.segment_id for segment in submitted}
        for segment in submitted:
            latest = now_by_id.get(segment.segment_id)
            if latest is None:
                return False
            if segment.final:
                if (
                    not latest.final
                    or latest.sequence != segment.sequence
                    or latest.text != segment.text
                ):
                    return False
            elif latest.final:
                if not latest.text.startswith(segment.text) or _correction_follows(
                    segment.text, latest.text
                ):
                    return False
            elif not latest.stable_text.startswith(segment.text) or _correction_follows(
                segment.text, latest.stable_text
            ):
                return False
        newest_submitted = max(segment.sequence for segment in submitted)
        if any(
            segment.sequence > newest_submitted
            and segment.segment_id not in submitted_ids
            and (
                _starts_correction(segment.text)
                if segment.final
                else bool(segment.stable_text and _starts_correction(segment.stable_text))
            )
            for segment in current.segments
        ):
            return False
        if any(
            segment.final and segment.sequence <= newest_submitted
            and segment.segment_id not in submitted_ids
            for segment in current.segments
        ):
            return False
        return not any(
            not segment.final
            and segment.sequence > newest_submitted
            and segment.stable_text
            and _starts_correction(segment.stable_text)
            for segment in current.segments
        )

    def has_correction_since(self, previous: "ContextSnapshot") -> bool:
        """Whether newly stabilized speech explicitly reverses a prior target cue."""
        if (
            self.session_id,
            self.state_version,
            self.epoch,
            self.stream_id,
            self.language,
        ) != (
            previous.session_id,
            previous.state_version,
            previous.epoch,
            previous.stream_id,
            previous.language,
        ):
            return False
        before = {segment.segment_id: segment for segment in previous.segments}
        old_watermark = max((segment.sequence for segment in previous.segments), default=0)
        for segment in self.segments:
            prior = before.get(segment.segment_id)
            if (
                prior is not None
                and not prior.final
                and not segment.final
                and prior.stable_text
                and segment.stable_text
                and _correction_follows(prior.stable_text, segment.stable_text)
            ):
                return True
            text = segment.text if segment.final else segment.stable_text
            if segment.sequence > old_watermark and text and _starts_correction(text):
                return True
        return False


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

    def invalidate(self) -> None:
        self._epoch += 1
        self._boundary_ns = time.monotonic_ns()
        self._segments.clear()
        self._stream = None
        self._sequence = 0

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
        self._stream, self._sequence = stream, sequence
        self._expire(now)
        final = cast(bool, event["final"])
        text = cast(str, event["text"])
        stable = text if final else self._stable_prefix(previous, text)
        self._segments[key] = ContextSegment(
            key,
            sequence,
            text,
            final,
            start,
            end,
            stamp,
            stable,
        )
        while (
            len(self._segments) > 64 or sum(len(s.text) + 1 for s in self._segments.values()) > 8192
        ):
            del self._segments[next(iter(self._segments))]

    @staticmethod
    def _stable_prefix(previous: ContextSegment | None, text: str) -> str:
        """Return whole tokens corroborated by consecutive interim hypotheses."""
        if previous is None or previous.final:
            return ""
        old_tokens, new_tokens = previous.text.split(), text.split()
        common: list[str] = []
        for old, new in zip(old_tokens, new_tokens, strict=False):
            if old != new:
                break
            common.append(old)
        stable_tokens = previous.stable_text.split()
        if stable_tokens and new_tokens[: len(stable_tokens)] != stable_tokens:
            stable_tokens = []
        if len(common) > len(stable_tokens):
            stable_tokens = common
        return " ".join(stable_tokens)

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
