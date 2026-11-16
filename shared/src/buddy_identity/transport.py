"""Bounded HTTPS requests without redirects, logging, retries, or raw exception messages."""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from buddy_identity.config import IdentityError, SessionExpired


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _is_expired_credential(status: int, headers: Any, body: bytes) -> bool:
    """Recognize only explicit credential rejection, not generic client errors."""
    if status == 401:
        return True
    if status != 400:
        return False

    def is_not_authorized(value: object) -> bool:
        return (
            isinstance(value, str)
            and value.rsplit("#", 1)[-1].rsplit(".", 1)[-1].split(":", 1)[0]
            == "NotAuthorizedException"
        )

    provider_error = ""
    if headers is not None:
        provider_error = str(headers.get("x-amzn-errortype", "")).split(":", 1)[0]
    if is_not_authorized(provider_error):
        return True
    try:
        if len(body) > 8192:
            return False
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    oauth_error = payload.get("error")
    aws_error = payload.get("__type")
    return oauth_error == "invalid_grant" or is_not_authorized(aws_error)


def request_json(request: Request, *, empty: bool = False) -> dict[str, Any]:
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError
            if empty and not data:
                return {}
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
    except HTTPError as exc:
        status = exc.code
        try:
            body = exc.read(8193)
            headers = exc.headers
        except OSError:
            body, headers = b"", None
        finally:
            exc.close()
        if _is_expired_credential(status, headers, body):
            raise SessionExpired("Sign-in expired or was rejected. Please sign in again.") from None
        if status == 429:
            raise IdentityError("Too many attempts. Wait a minute before trying again.") from None
        raise IdentityError("Account service unavailable. Please try again later.") from None
    except (URLError, OSError, ValueError):
        raise IdentityError(
            "Account service unavailable or returned an invalid response."
        ) from None
