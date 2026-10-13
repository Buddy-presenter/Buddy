"""Validate runtime configuration before creating the Qt application."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """A recoverable environment configuration error."""


def validate_api_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError("BUDDY_API_URL must be a valid HTTP(S) URL.") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(char.isspace() or ord(char) < 32 for char in value)
        or port == 0
    ):
        raise ConfigurationError(
            "BUDDY_API_URL needs an HTTP(S) host, without credentials, query, or fragment."
        )
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("Use HTTPS for remote services; HTTP is for loopback only.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class AppConfig:
    api_url: str
    request_timeout: float = 5.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "AppConfig":
        values = os.environ if environ is None else environ
        url = validate_api_url(values.get("BUDDY_API_URL", "http://127.0.0.1:8000"))
        try:
            timeout = float(values.get("BUDDY_REQUEST_TIMEOUT_SECONDS", "5"))
        except ValueError as exc:
            raise ConfigurationError("Request timeout must be a number from 0.1 to 30.") from exc
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
            raise ConfigurationError("Request timeout must be a number from 0.1 to 30.")
        return cls(url, timeout)
