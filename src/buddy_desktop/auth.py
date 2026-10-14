"""Desktop authorization-code/PKCE lifecycle; only refresh tokens enter the OS vault."""

import base64
import hashlib
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event
from typing import Protocol, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from keyring.backend import KeyringBackend

from buddy_desktop.config import validate_api_url
from buddy_identity.config import CALLBACK, SCOPES, IdentityConfig, IdentityError, SessionExpired
from buddy_identity.transport import request_json


class TokenStore(Protocol):
    def read(self) -> str | None: ...
    def save(self, token: str, *, fresh: bool = False) -> None: ...
    def block(self) -> None: ...
    def delete(self) -> None: ...


class NativeTokenStore:
    def __init__(self, identity: str, directory: Path) -> None:
        digest = hashlib.sha256(identity.encode()).hexdigest()
        self.service = "Buddy/" + digest
        self.marker = directory / ("signed-out-" + digest)

    def backend(self) -> KeyringBackend:
        try:
            # Explicit native implementations; no auto-discovered plaintext or chained fallback.
            if sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring

                return cast(Callable[[], KeyringBackend], WinVaultKeyring)()
            if sys.platform == "darwin":
                from keyring.backends.macOS import Keyring

                return cast(Callable[[], KeyringBackend], Keyring)()
        except Exception:
            pass
        raise IdentityError("The operating-system credential store is unavailable.")

    def read(self) -> str | None:
        try:
            if self.marker.exists():
                return None
            return self.backend().get_password(self.service, "refresh")
        except Exception:
            raise IdentityError("Cannot read the credential store. Please sign in again.") from None

    def save(self, token: str, *, fresh: bool = False) -> None:
        try:
            self.backend().set_password(self.service, "refresh", token)
            if fresh:
                self.marker.unlink(missing_ok=True)
        except Exception:
            raise IdentityError("Cannot save the session in the OS credential store.") from None

    def block(self) -> None:
        try:
            # A non-secret tombstone prevents restore when credential deletion failed.
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text("signed out\n", encoding="utf-8")
        except OSError:
            raise IdentityError(
                "Cannot record sign-out locally. Check your profile's write access."
            ) from None

    def delete(self) -> None:
        try:
            backend = self.backend()
            if backend.get_password(self.service, "refresh") is not None:
                backend.delete_password(self.service, "refresh")
        except Exception:
            raise IdentityError(
                "Cannot remove the saved session. Resume remains disabled."
            ) from None


def browser_code(
    url: str,
    state: str,
    cancelled: Event,
    *,
    timeout: float = 300,
    opener: Callable[[str], bool] = webbrowser.open,
    logout: bool = False,
) -> str:
    outcome: list[str] = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            valid = False
            try:
                parts = urlsplit(self.path)
                values = parse_qs(parts.query, max_num_fields=8)
                valid = (
                    len(self.path) < 8192
                    and parts.path == "/callback"
                    and self.headers.get("Host") == "localhost:8765"
                    and (
                        (logout and not values)
                        or (
                            not logout
                            and values.get("state") == [state]
                            and (len(values.get("code", [])) == 1 or "error" in values)
                        )
                    )
                )
                if valid:
                    outcome.append(
                        "signedout" if logout else ("" if "error" in values else values["code"][0])
                    )
            except ValueError:
                pass
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Return to Buddy." if valid else b"Callback rejected.")

        def setup(self) -> None:
            self.request.settimeout(2)
            super().setup()

    try:
        with HTTPServer(("127.0.0.1", 8765), Callback) as server:
            server.timeout = 0.2
            if not opener(url):
                raise IdentityError("Cannot open the system browser. Check your default browser.")
            deadline = time.monotonic() + timeout
            while not outcome and not cancelled.is_set() and time.monotonic() < deadline:
                server.handle_request()
    except OSError:
        raise IdentityError(
            "Cannot start sign-in. Close another Buddy sign-in and retry."
        ) from None
    if cancelled.is_set() or not outcome or not outcome[0]:
        raise IdentityError("Sign-in was cancelled, rejected, or timed out.")
    return outcome[0]


@dataclass(repr=False)
class Tokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires: float


class AccountSession:
    def __init__(self, config: IdentityConfig, api_url: str, store: TokenStore) -> None:
        self.config = config
        self.api_url = validate_api_url(api_url)
        self.store = store
        self.tokens: Tokens | None = None
        self.pending_revocation: str | None = None
        self.cancelled = Event()

    def token_request(self, values: dict[str, str]) -> Tokens:
        result = request_json(
            Request(
                self.config.domain + "/oauth2/token",
                data=urlencode({"client_id": self.config.client_id, **values}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )
        access, refresh, lifetime = (
            result.get("access_token"),
            result.get("refresh_token"),
            result.get("expires_in"),
        )
        if (
            result.get("token_type", "").lower() != "bearer"
            or not isinstance(access, str)
            or not 1 <= len(access) <= 16300
            or not isinstance(refresh, str)
            or not 1 <= len(refresh) <= 16300
            or type(lifetime) is not int
            or not 60 <= lifetime <= 86400
        ):
            raise IdentityError("Account service returned an invalid session.")
        return Tokens(access, refresh, time.monotonic() + lifetime)

    def revoke(self, refresh: str) -> None:
        request_json(
            Request(
                self.config.domain + "/oauth2/revoke",
                data=urlencode({"client_id": self.config.client_id, "token": refresh}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            empty=True,
        )

    def accept(self, tokens: Tokens, *, fresh: bool) -> str:
        try:
            if self.cancelled.is_set():
                raise IdentityError("Sign-in was cancelled.")
            self.store.save(tokens.refresh, fresh=fresh)
        except IdentityError:
            with suppress(IdentityError):
                self.revoke(tokens.refresh)
            self.tokens = None
            raise
        self.tokens = tokens
        try:
            return self.account()
        except IdentityError:
            self.tokens = None
            raise

    def sign_in(self, action: str = "signin") -> str:
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        endpoint = {
            "signin": "/oauth2/authorize",
            "signup": "/signup",
            "recovery": "/forgotPassword",
        }[action]
        parameters = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": CALLBACK,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "prompt": "login",
        }
        code = browser_code(
            self.config.domain + endpoint + "?" + urlencode(parameters), state, self.cancelled
        )
        return self.accept(
            self.token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CALLBACK,
                    "code_verifier": verifier,
                }
            ),
            fresh=True,
        )

    def resume(self) -> str:
        refresh = self.store.read()
        if not refresh:
            raise SessionExpired("No saved session. Please sign in.")
        try:
            return self.accept(
                self.token_request({"grant_type": "refresh_token", "refresh_token": refresh}),
                fresh=False,
            )
        except SessionExpired:
            self.tokens = None
            self.store.block()
            self.store.delete()
            raise

    def account(self) -> str:
        if self.tokens is None:
            raise SessionExpired("Please sign in.")
        if time.monotonic() >= self.tokens.expires - 30:
            self.tokens = None
            return self.resume()
        result = request_json(
            Request(
                self.api_url.rstrip("/") + "/api/v1/auth/me",
                headers={
                    "Authorization": "Bearer " + self.tokens.access,
                    "Accept": "application/json",
                },
            )
        )
        try:
            from uuid import UUID

            return str(UUID(result["data"]["id"]))
        except (KeyError, TypeError, ValueError):
            raise IdentityError("Account service returned an invalid account.") from None

    def sign_out(self) -> str:
        refresh = self.tokens.refresh if self.tokens else self.pending_revocation
        self.tokens = None
        errors: list[str] = []
        if refresh is None:
            try:
                refresh = self.store.read()
            except IdentityError as exc:
                errors.append(str(exc))
        for operation in (self.store.block, self.store.delete):
            try:
                operation()
            except IdentityError as exc:
                errors.append(str(exc))
        if refresh:
            try:
                self.revoke(refresh)
                self.pending_revocation = None
            except IdentityError:
                self.pending_revocation = refresh
                errors.append(
                    "Local sign-out completed, but online revocation failed. Retry when connected."
                )
        if errors:
            raise IdentityError(" ".join(errors))
        # This URL contains public client metadata only. Tokens never enter the browser.
        browser_code(
            self.config.domain
            + "/logout?"
            + urlencode(
                {
                    "client_id": self.config.client_id,
                    "logout_uri": CALLBACK,
                }
            ),
            "",
            self.cancelled,
            timeout=30,
            logout=True,
        )
        return "Signed out."
