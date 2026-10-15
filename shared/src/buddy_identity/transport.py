"""Bounded HTTPS requests without redirects, logging, retries, or raw exception messages."""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from buddy_identity.config import IdentityError, SessionExpired


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


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
        exc.close()
        if status in {400, 401, 403}:
            raise SessionExpired("Sign-in expired or was rejected. Please sign in again.") from None
        if status == 429:
            raise IdentityError("Too many attempts. Wait a minute before trying again.") from None
        raise IdentityError("Account service unavailable. Please try again later.") from None
    except (URLError, OSError, ValueError):
        raise IdentityError(
            "Account service unavailable or returned an invalid response."
        ) from None
