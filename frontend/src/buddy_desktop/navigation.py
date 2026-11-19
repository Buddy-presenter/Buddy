"""Conservative local recognition for clear slide navigation commands."""

import re
import unicodedata


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"\blet['’]s\b", "let us", normalized)
    return re.findall(r"[+-]?\d+|[^\W\d_]+", normalized)


def _safe_continuation(tokens: list[str]) -> bool:
    if not tokens:
        return True
    unsafe = {
        "if",
        "unless",
        "until",
        "only",
        "provided",
        "assuming",
        "depending",
        "not",
        "no",
        "never",
        "cannot",
        "without",
        "actually",
        "instead",
        "rather",
        "sorry",
        "wait",
        "cancel",
        "forget",
        "scratch",
        "but",
        "or",
    }
    if unsafe.intersection(tokens):
        return False
    if any(tokens[i : i + 2] == ["do", "not"] for i in range(len(tokens) - 1)):
        return False
    return tokens[0] in {
        "and",
        "as",
        "because",
        "since",
        "so",
        "then",
        "where",
        "which",
        "while",
        "we",
        "i",
        "you",
        "it",
        "this",
        "that",
        "the",
        "now",
        "um",
        "uh",
        "please",
    }


_CARDINALS = {
    **dict(
        zip(
            [
                "zero",
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
            ],
            range(20),
            strict=True,
        )
    ),
    "twenty": 20,
    "thirty": 30,
}
_ORDINALS = dict(
    zip(
        [
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
            "eleventh",
            "twelfth",
            "thirteenth",
            "fourteenth",
            "fifteenth",
            "sixteenth",
            "seventeenth",
            "eighteenth",
            "nineteenth",
            "twentieth",
            "thirtieth",
        ],
        (*range(1, 21), 30),
        strict=True,
    )
)


def _number(tokens: list[str]) -> int | None:
    if len(tokens) == 1:
        if re.fullmatch(r"[+-]?\d{1,9}", tokens[0]):
            return int(tokens[0])
        ordinal = re.fullmatch(r"(\d{1,3})(?:st|nd|rd|th)", tokens[0])
        if ordinal:
            value = int(ordinal.group(1))
            return value if value > 0 else None
        if tokens[0] in _CARDINALS:
            return _CARDINALS[tokens[0]]
        return _ORDINALS.get(tokens[0])
    if len(tokens) == 2:
        tens = {"twenty": 20, "thirty": 30}
        unit = (
            _ORDINALS.get(tokens[1])
            if tokens[1].endswith(("st", "nd", "rd", "th"))
            else _CARDINALS.get(tokens[1])
        )
        if tokens[0] in tens and unit is not None and 1 <= unit <= 9:
            return tens[tokens[0]] + unit % 10
    return None


def navigation_intent(text: str) -> tuple[str, int] | None:
    """Return a slide move only for an unambiguous imperative; otherwise defer to server."""
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    if any(char in normalized for char in '?？"“”‘’«»'):
        return None
    tokens = _tokens(normalized)
    if not tokens:
        return None
    # Relative navigation. Keep the recognized command prefix small and strict.
    relative_patterns = (
        (
            r"^(?:please )?(?:let us )?"
            r"(?:(?:go|move|switch|advance) to (?:the )?)?"
            r"(next|previous) (?:slide|page)",
        ),
        (
            r"^(?:please )?(?:let us )?"
            r"(?:go|move) (forward|back|backward) (?:by )?one (?:slide|page)",
        ),
        (r"^(?:please )?(?:let us )?go back one (?:slide|page)",),
    )
    phrase = " ".join(tokens)
    for (pattern,) in relative_patterns:
        match = re.match(pattern, phrase)
        if match and _safe_continuation(tokens[len(_tokens(match.group(0))) :]):
            direction = match.group(1) if match.lastindex else "back"
            return ("relative", 1 if direction in {"next", "forward"} else -1)

    # Ordinal before slide/page: "go back to the first slide".
    ordinal = re.match(
        r"^(?:please )?(?:let us )?"
        r"(?:go|move|jump|navigate|switch|return) (?:back )?to "
        r"(?:the )?(.+?) (?:slide|page)",
        phrase,
    )
    if ordinal and _safe_continuation(tokens[len(_tokens(ordinal.group(0))) :]):
        value = _number(ordinal.group(1).split())
        if value is not None and value > 0:
            return ("absolute", value)

    prefixes = (
        r"^(?:please )?(?:let us )?"
        r"(?:go|move|jump|navigate|switch|turn) (?:to|over to) "
        r"(?:the )?(?:slide|page) (?:number )?",
        r"^(?:please )?(?:let us )?(?:go|move|return) back to (?:the )?(?:slide|page) (?:number )?",
        r"^(?:please )?show (?:me )?(?:the )?(?:slide|page) (?:number )?",
        r"^(?:please )?take me to (?:the )?(?:slide|page) (?:number )?",
        r"^(?:please )?i want to (?:go|move|jump) to (?:the )?(?:slide|page) (?:number )?",
    )
    for prefix in prefixes:
        match = re.match(prefix + r"(.+)", phrase)
        if not match:
            continue
        number_words = match.group(1).split()
        for end in range(len(number_words), 0, -1):
            value = _number(number_words[:end])
            if value is not None and _safe_continuation(number_words[end:]):
                return ("absolute", value)
    return None
