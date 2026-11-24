"""Explicit, local voice commands for opening a web search and returning to Buddy."""

import re
from collections import deque
from contextlib import suppress
from dataclasses import dataclass


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


_SEARCH = re.compile(
    r"^(?:please\s+)?(?:(?:open\s+(?:google\s+)?chrome\s+and\s+)?search\s+for)\s+(?P<query>.+)$",
    re.IGNORECASE,
)
_IMAGE_QUERY = re.compile(r"^images?\s+of\s+(.+)$", re.IGNORECASE)
_RETURN = re.compile(
    r"^(?:return\s+to\s+(?:the\s+)?presentation"
    r"|go\s+back\s+to\s+(?:the\s+)?presentation"
    r"|let'?s\s+go\s+back\s+to\s+(?:the\s+)?presentation"
    r"|let\s+us\s+go\s+back\s+to\s+(?:the\s+)?presentation)"
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
_RETURN_PREFIXES = (
    "return to the presentation",
    "return to presentation",
    "go back to the presentation",
    "go back to presentation",
    "let's go back to the presentation",
    "let's go back to presentation",
    "lets go back to the presentation",
    "lets go back to presentation",
    "let us go back to the presentation",
    "let us go back to presentation",
)


def _clean_text(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).strip(" \t\r\n.,!?;:")


def _remove_wake_word(text: str) -> str:
    return re.sub(r"^buddy(?:[\s,]+)", "", text, count=1, flags=re.IGNORECASE).strip()


def _is_explicit_command_prefix(text: str) -> bool:
    """Recognize an in-progress command so its partial text is not sent to the LLM."""
    normalized = _remove_wake_word(_clean_text(text)).casefold()
    if len(normalized) < 4:
        return False
    # Don't suppress common opening words such as “please” or “open Chrome”.
    # Suppression starts only after the complete unique search trigger is spoken.
    search_prefix = any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in _SEARCH_PREFIXES
    )
    # The words “go back to” are also used for deterministic slide navigation.
    # Do not claim that speech until it explicitly names the presentation.
    return_prefix = any(
        normalized == phrase or normalized.startswith(phrase + " ")
        for phrase in _RETURN_PREFIXES
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
    match = _SEARCH.fullmatch(normalized)
    if match is None:
        return None
    query = _clean_text(match.group("query"))
    if not query or len(query) > 300 or any(ord(char) < 32 for char in query):
        return None
    image_match = _IMAGE_QUERY.fullmatch(query)
    if image_match is not None:
        query = _clean_text(image_match.group(1))
        if not query:
            return None
        return WebSearch(query, images=True)
    return WebSearch(query)


class VoiceCommandRouter:
    """Route final explicit commands locally and keep their transcript out of the agent."""

    def __init__(self) -> None:
        self._command_segments: deque[str] = deque(maxlen=16)

    def handle(self, event: dict[str, object]) -> VoiceCommandResult:
        text = event.get("text")
        if not isinstance(text, str):
            return VoiceCommandResult(False)
        segment_id = event.get("segment_id")
        segment = segment_id if isinstance(segment_id, str) else ""
        final = event.get("final") is True
        prefix = _is_explicit_command_prefix(text)
        remembered = bool(segment and segment in self._command_segments)
        suppress_agent = prefix or remembered
        if not final:
            if prefix and segment and segment not in self._command_segments:
                self._command_segments.append(segment)
            return VoiceCommandResult(suppress_agent)
        if segment:
            with suppress(ValueError):
                self._command_segments.remove(segment)
        command = parse_voice_command(text) if suppress_agent or prefix else None
        return VoiceCommandResult(suppress_agent or command is not None, command)
