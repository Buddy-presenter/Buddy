"""Explicit, local voice commands for opening a web search and returning to Buddy."""

import re
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

from .navigation import navigation_intent


@dataclass(frozen=True)
class WebSearch:
    query: str
    images: bool = False


@dataclass(frozen=True)
class ReturnToPresentation:
    pass


VoiceCommand = WebSearch | ReturnToPresentation


@dataclass(frozen=True)
class VoiceCommandResult:
    """Whether an update belongs to an explicit command and must skip the agent."""

    suppress_agent: bool
    command: VoiceCommand | None = None
    correction_pending: bool = False
    correction_waiting_for_query: bool = False
    search_pending: bool = False


_SEARCH = re.compile(
    r"^(?:please\s+)?(?:(?:open\s+(?:google\s+)?chrome\s+and\s+)?search\s+for)\s*(?P<query>.*)$",
    re.IGNORECASE,
)
_IMAGE_QUERY = re.compile(r"^images?\s+of(?:\s+(?P<query>.*))?$", re.IGNORECASE)
_IMAGE_ONLY = re.compile(r"^images?$", re.IGNORECASE)
_CORRECTION_MARKER = re.compile(
    r"\b(?:i\s+mean|i\s+meant|make\s+that|scratch\s+that)\b"
    r"|[,;:—–-]\s*actually\b"
    r"|,\s*(?:no|sorry)\b",
    re.IGNORECASE,
)
_CORRECTION_ONLY = re.compile(
    r"^(?:i\s+mean|i\s+meant|actually|rather|make\s+that|scratch\s+that"
    r"|sorry\s*,?\s+i\s+(?:mean|meant)|no\s*,|sorry\s*,)"
    r"\s*[,;:—–-]?\s*(?P<query>.+)$",
    re.IGNORECASE,
)
_CORRECTION_PREFIX = re.compile(
    r"^(?:i\s+mean|i\s+meant|actually|rather|make\s+that|scratch\s+that"
    r"|sorry(?:\s*,?\s+i\s+(?:mean|meant))?|no\s*,?)\b",
    re.IGNORECASE,
)
_RETURN_TARGET = r"(?:(?:the|my)\s+)?(?:presentation|slides|pdf|deck|buddy(?:\s+app)?)"
_RETURN = re.compile(
    rf"^(?:(?:please\s+)?(?:return|go\s+back|switch\s+back|take\s+me\s+back)\s+to\s+{_RETURN_TARGET}"
    rf"|let'?s\s+(?:go\s+back|return)\s+to\s+{_RETURN_TARGET}"
    rf"|let\s+us\s+(?:go\s+back|return)\s+to\s+{_RETURN_TARGET}"
    rf"|back\s+to\s+{_RETURN_TARGET})"
    r"(?:\s+please)?$",
    re.IGNORECASE,
)
_SEARCH_PREFIXES = (
    "please search for",
    "please open chrome and search for",
    "please open google chrome and search for",
    "search for",
    "open chrome and search for",
    "open google chrome and search for",
)
_RETURN_TARGETS = (
    "the presentation",
    "presentation",
    "my presentation",
    "the slides",
    "slides",
    "my slides",
    "the pdf",
    "pdf",
    "my pdf",
    "the deck",
    "deck",
    "my deck",
    "Buddy",
    "the Buddy app",
)
_RETURN_PREFIXES = tuple(
    f"{verb} to {target}"
    for verb in (
        "return",
        "please return",
        "go back",
        "please go back",
        "switch back",
        "take me back",
        "let's go back",
        "lets go back",
        "let us go back",
        "let's return",
        "lets return",
        "let us return",
        "back",
    )
    for target in _RETURN_TARGETS
)
_SLIDE_NAVIGATION = re.compile(
    r"^(?:(?:please\s+)?(?:(?:let'?s|let\s+us)\s+)?(?:i\s+want\s+to\s+)?"
    r"(?:go|jump|move|navigate|switch|return)\s+(?:back\s+)?to\s+(?:the\s+)?"
    r"(?:slide|page)\b|"
    r"(?:please\s+)?(?:(?:let'?s|let\s+us)\s+)?(?:i\s+want\s+to\s+)?"
    r"(?:go|jump|move|navigate|switch|return)\s+(?:back\s+)?to\s+(?:the\s+)?"
    r"(?:first|second|third|\d+(?:st|nd|rd|th)?)\s+(?:slide|page)\b|"
    r"(?:please\s+)?(?:go|jump|move|navigate|switch)\s+to\s+(?:the\s+)?slide\b|"
    r"(?:go\s+to\s+)?(?:next|previous|prior|first|last)\s+slide\b)",
    re.IGNORECASE,
)
_NON_CONTENT_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "anything",
        "for",
        "image",
        "images",
        "of",
        "or",
        "photo",
        "photos",
        "picture",
        "pictures",
        "please",
        "search",
        "something",
        "the",
        "to",
        "whatever",
    }
)


def _clean_text(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).strip(" \t\r\n.,!?;:")


def _remove_wake_word(text: str) -> str:
    return re.sub(r"^buddy(?:[\s,]+)", "", text, count=1, flags=re.IGNORECASE).strip()


def _corrected_query(query: str) -> tuple[str, bool]:
    """Use the last explicit self-correction in a search phrase, if present."""
    markers = tuple(
        marker
        for marker in _CORRECTION_MARKER.finditer(query)
        if _is_explicit_inline_correction(query, marker)
    )
    if not markers:
        return _clean_text(query), False
    return _clean_text(query[markers[-1].end() :].lstrip(" \t,;:—–-")), True


def _is_explicit_inline_correction(query: str, marker: re.Match[str]) -> bool:
    """Avoid treating ordinary explanatory uses of “I mean” as corrections."""
    if re.fullmatch(r"i\s+mean(?:t)?", marker.group().strip(), flags=re.IGNORECASE) is None:
        return True
    suffix = _clean_text(query[marker.end() :])
    first_word = re.match(r"[\w]+", suffix, flags=re.UNICODE)
    if first_word is None:
        return query[: marker.start()].rstrip().endswith((",", ";", ":", "—", "–", "-"))
    return first_word.group().casefold() not in {
        "as",
        "by",
        "for",
        "how",
        "is",
        "of",
        "that",
        "to",
        "when",
        "where",
        "whether",
        "why",
    }


def _has_concrete_content(query: str) -> bool:
    words = re.findall(r"[\w]+(?:['’-][\w]+)*", query, flags=re.UNICODE)
    return any(word.casefold() not in _NON_CONTENT_WORDS for word in words)


def _search_parts(text: str) -> tuple[bool, str, bool] | None:
    """Return (image search, query text, ends in an unfinished correction)."""
    normalized = _remove_wake_word(_clean_text(text))
    match = _SEARCH.fullmatch(normalized)
    if match is None:
        return None
    raw_query = _clean_text(match.group("query"))
    image_match = _IMAGE_QUERY.fullmatch(raw_query)
    images = image_match is not None or _IMAGE_ONLY.fullmatch(raw_query) is not None
    if image_match is not None:
        raw_query = image_match.group("query") or ""
    elif images:
        raw_query = ""
    query, was_corrected = _corrected_query(raw_query)
    correction_incomplete = was_corrected and not query
    return images, query, correction_incomplete


def _continuation_query(text: str, *, images: bool) -> tuple[str, bool]:
    """Interpret words following an explicitly spoken but incomplete search command."""
    query = _remove_wake_word(_clean_text(text))
    if _CORRECTION_PREFIX.fullmatch(query) is not None:
        return "", images
    corrected = _correction_fragment(query)
    if corrected is not None:
        query = corrected
    image_match = _IMAGE_QUERY.fullmatch(query)
    if image_match is not None:
        images = True
        query = image_match.group("query") or ""
    elif _IMAGE_ONLY.fullmatch(query) is not None:
        images = True
        query = ""
    return _clean_text(query), images


def _correction_fragment(text: str) -> str | None:
    match = _CORRECTION_ONLY.fullmatch(_clean_text(text))
    if match is None:
        return None
    query = _clean_text(match.group("query").lstrip(" \t,;:—–-"))
    return query or None


def _is_explicit_command_prefix(text: str) -> bool:
    """Recognize an in-progress command so its partial text is not sent to the LLM."""
    normalized = _remove_wake_word(_clean_text(text)).casefold()
    if len(normalized) < 4:
        return False
    # Don't suppress common opening words such as “please” or “open Chrome”.
    # Suppression starts only after the complete unique search trigger is spoken.
    search_prefix = any(
        normalized == prefix or normalized.startswith(prefix + " ") for prefix in _SEARCH_PREFIXES
    )
    # The words “go back to” are also used for deterministic slide navigation.
    # Do not claim that speech until it explicitly names the presentation.
    return_prefix = any(
        normalized == phrase or normalized.startswith(phrase + " ") for phrase in _RETURN_PREFIXES
    )
    return search_prefix or return_prefix


def parse_voice_command(text: str) -> VoiceCommand | None:
    """Parse only clear, leading spoken commands; ordinary sentence mentions do not match."""
    normalized = _clean_text(text)
    if not normalized:
        return None
    normalized = _remove_wake_word(normalized)
    if _RETURN.fullmatch(normalized):
        return ReturnToPresentation()
    parts = _search_parts(normalized)
    if parts is None:
        return None
    images, query, correction_incomplete = parts
    if (
        correction_incomplete
        or not _has_concrete_content(query)
        or len(query) > 300
        or any(ord(char) < 32 for char in query)
    ):
        return None
    return WebSearch(query, images=images)


class VoiceCommandRouter:
    """Route final explicit commands locally and keep their transcript out of the agent."""

    _INCOMPLETE_SEARCH_SECONDS = 4.0
    _CORRECTION_SECONDS = 1.8

    def __init__(self) -> None:
        self._command_segments: deque[str] = deque(maxlen=16)
        self._correction_segments: deque[str] = deque(maxlen=16)
        self._intent_segments: deque[str] = deque(maxlen=16)
        self._recent_search: WebSearch | None = None
        self._recent_search_at = 0.0
        self._pending_search_images = False
        self._pending_search_at = 0.0
        self._correction_waiting_for_query = False
        self._correction_waiting_at = 0.0

    def _recent_search_is_correctable(self) -> bool:
        return self._recent_search is not None and monotonic() - self._recent_search_at <= 0.5

    def _expire_pending(self, now: float) -> None:
        if now - self._pending_search_at > self._INCOMPLETE_SEARCH_SECONDS:
            self._pending_search_images = False
            self._pending_search_at = 0.0
            self._intent_segments.clear()
        if now - self._correction_waiting_at > self._CORRECTION_SECONDS:
            self._correction_waiting_for_query = False
            self._correction_waiting_at = 0.0
            if self._recent_search is not None and now - self._recent_search_at > 0.5:
                self._recent_search = None

    def _remember_search(self, command: WebSearch, now: float) -> None:
        self._recent_search = command
        self._recent_search_at = now
        self._correction_waiting_for_query = False
        self._correction_waiting_at = 0.0
        self._pending_search_images = False
        self._pending_search_at = 0.0
        self._intent_segments.clear()

    def _clear_search_state(self) -> None:
        self._recent_search = None
        self._recent_search_at = 0.0
        self._correction_waiting_for_query = False
        self._correction_waiting_at = 0.0
        self._pending_search_images = False
        self._pending_search_at = 0.0
        self._intent_segments.clear()

    def handle(self, event: dict[str, object]) -> VoiceCommandResult:
        text = event.get("text")
        if not isinstance(text, str):
            return VoiceCommandResult(False)
        now = monotonic()
        self._expire_pending(now)
        segment_id = event.get("segment_id")
        segment = segment_id if isinstance(segment_id, str) else ""
        final = event.get("final") is True
        prefix = _is_explicit_command_prefix(text)
        remembered = bool(segment and segment in self._command_segments)
        correction_remembered = bool(segment and segment in self._correction_segments)
        intent_remembered = bool(segment and segment in self._intent_segments)
        correction_marker = (
            _CORRECTION_PREFIX.match(_clean_text(_remove_wake_word(text))) is not None
        )
        correction_prefix = self._recent_search_is_correctable() and correction_marker
        correction_followup = self._correction_waiting_for_query and (
            now - self._correction_waiting_at <= self._CORRECTION_SECONDS
        )
        search_continuation = (
            self._pending_search_at > 0
            and now - self._pending_search_at <= self._INCOMPLETE_SEARCH_SECONDS
        )
        suppress_agent = (
            prefix
            or remembered
            or correction_prefix
            or correction_remembered
            or correction_followup
            or search_continuation
            or intent_remembered
        )
        if not final:
            if prefix and segment and segment not in self._command_segments:
                self._command_segments.append(segment)
            if (correction_prefix or correction_followup) and segment:
                if segment not in self._correction_segments:
                    self._correction_segments.append(segment)
                self._correction_waiting_for_query = True
                self._correction_waiting_at = now
            if search_continuation and segment and segment not in self._intent_segments:
                self._intent_segments.append(segment)
            return VoiceCommandResult(
                suppress_agent,
                correction_pending=correction_prefix or correction_followup,
                correction_waiting_for_query=correction_prefix or correction_followup,
                search_pending=search_continuation,
            )
        if segment:
            with suppress(ValueError):
                self._command_segments.remove(segment)
            with suppress(ValueError):
                self._correction_segments.remove(segment)
            with suppress(ValueError):
                self._intent_segments.remove(segment)

        command = parse_voice_command(text) if prefix or remembered else None
        if isinstance(command, ReturnToPresentation):
            self._clear_search_state()
            return VoiceCommandResult(True, command)

        parts = _search_parts(text) if prefix or remembered else None
        if parts is not None:
            images, query, correction_incomplete = parts
            if _has_concrete_content(query) and len(query) <= 300:
                command = WebSearch(query, images=images)
                self._remember_search(command, now)
                return VoiceCommandResult(True, command)
            if correction_incomplete:
                self._recent_search = WebSearch("", images=images)
                self._recent_search_at = now
                self._correction_waiting_for_query = True
                self._correction_waiting_at = now
                return VoiceCommandResult(
                    True,
                    correction_pending=True,
                    correction_waiting_for_query=True,
                    search_pending=True,
                )
            self._clear_search_state()
            self._pending_search_images = images
            self._pending_search_at = now
            return VoiceCommandResult(True, search_pending=True)

        if (correction_prefix or correction_remembered) and self._recent_search is not None:
            corrected = _correction_fragment(_remove_wake_word(text))
            if corrected is not None and _has_concrete_content(corrected) and len(corrected) <= 300:
                command = WebSearch(corrected, images=self._recent_search.images)
                self._remember_search(command, now)
                return VoiceCommandResult(True, command)
            self._correction_waiting_for_query = True
            self._correction_waiting_at = now
            self._recent_search_at = now
            return VoiceCommandResult(
                True,
                correction_pending=True,
                correction_waiting_for_query=True,
            )

        if correction_followup and self._recent_search is not None:
            corrected, images = _continuation_query(text, images=self._recent_search.images)
            if _has_concrete_content(corrected) and len(corrected) <= 300:
                command = WebSearch(corrected, images=images)
                self._remember_search(command, now)
                return VoiceCommandResult(True, command)
            self._correction_waiting_at = now
            self._recent_search_at = now
            return VoiceCommandResult(
                True,
                correction_pending=True,
                correction_waiting_for_query=True,
            )

        if search_continuation or intent_remembered:
            if navigation_intent(text) is not None or _SLIDE_NAVIGATION.match(
                _clean_text(_remove_wake_word(text))
            ):
                self._pending_search_images = False
                self._pending_search_at = 0.0
                self._intent_segments.clear()
                return VoiceCommandResult(False)
            query, images = _continuation_query(text, images=self._pending_search_images)
            if _has_concrete_content(query) and len(query) <= 300:
                command = WebSearch(query, images=images)
                self._remember_search(command, now)
                return VoiceCommandResult(True, command)
            self._pending_search_at = now
            return VoiceCommandResult(True, search_pending=True)

        if final:
            self._clear_search_state()
        return VoiceCommandResult(suppress_agent or command is not None, command)
