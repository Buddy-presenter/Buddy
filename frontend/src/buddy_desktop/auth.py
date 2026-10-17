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
from pathlib import Path
from threading import Event
from typing import Protocol, cast
from urllib.parse import urlencode
from urllib.request import Request

from keyring.backend import KeyringBackend

from buddy_desktop.config import validate_api_url
from buddy_desktop.native_callback import pending_callback
from buddy_identity.config import (
    CALLBACK,
    SCOPES,
    SIGNOUT_CALLBACK,
    IdentityConfig,
    IdentityError,
    SessionExpired,
)
from buddy_identity.transport import request_json


class TokenStore(Protocol):
    def read(self) -> str | None: ...
    def save(self, token: str, *, fresh: bool = False) -> None: ...
    def block(self) -> None: ...
    def delete(self) -> None: ...


class NativeTokenStore:
    # Windows stores keyring strings as UTF-16 in a 2,560-byte credential blob.
    # Even 512 non-BMP characters fit (2,048 bytes). Bound the complete token to
    # the same 16,300-character limit accepted by AccountSession.token_request.
    _chunk_size = 512
    _max_chunks = 32
    _prefix = "Buddy-refresh-v1:"

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
            backend = self.backend()
            value = backend.get_password(self.service, "refresh")
            if sys.platform != "win32" or value is None or not value.startswith("Buddy-refresh-"):
                return value  # Existing single-entry sessions remain readable.
            if not value.startswith(self._prefix):
                raise ValueError("Unsupported credential format")
            count_text, digest = value.removeprefix(self._prefix).split(":")
            count = int(count_text)
            if not 1 <= count <= self._max_chunks or len(digest) != 64:
                raise ValueError("Invalid credential metadata")
            chunks = []
            for index in range(count):
                chunk = backend.get_password(self._chunk_service(index), "refresh")
                if not chunk or len(chunk) > self._chunk_size:
                    raise ValueError("Incomplete credential")
                chunks.append(chunk)
            token = "".join(chunks)
            if (
                len(token) > 16300
                or hashlib.sha256(token.encode()).hexdigest() != digest
                or backend.get_password(self.service, "refresh") != value
                or self.marker.exists()
            ):
                raise ValueError("Incomplete credential")
            return token
        except Exception:
            raise IdentityError("Cannot read the credential store. Please sign in again.") from None

    def save(self, token: str, *, fresh: bool = False) -> None:
        try:
            backend = self.backend()
            if sys.platform == "win32":
                if not 1 <= len(token) <= 16300:
                    raise ValueError("Unsupported credential length")
                # Invalidate the previous value before touching any pieces. Only
                # publish a readable manifest after all writes and cleanup succeed.
                backend.set_password(self.service, "refresh", self._prefix + "pending")
                chunks = [
                    token[offset : offset + self._chunk_size]
                    for offset in range(0, len(token), self._chunk_size)
                ]
                for index, chunk in enumerate(chunks):
                    backend.set_password(self._chunk_service(index), "refresh", chunk)
                for index in range(len(chunks), self._max_chunks):
                    self._delete_entry(backend, self._chunk_service(index))
                digest = hashlib.sha256(token.encode()).hexdigest()
                backend.set_password(
                    self.service, "refresh", f"{self._prefix}{len(chunks)}:{digest}"
                )
            else:
                backend.set_password(self.service, "refresh", token)
            if fresh:
                self.marker.unlink(missing_ok=True)
        except Exception:
            # A failed write must never make an older or partial session resumable.
            with suppress(IdentityError):
                self.block()
            with suppress(IdentityError):
                self.delete()
            raise IdentityError("Cannot save the session in the OS credential store.") from None

    def _chunk_service(self, index: int) -> str:
        return f"{self.service}/refresh-v1/{index}"

    @staticmethod
    def _delete_entry(backend: KeyringBackend, service: str) -> None:
        if backend.get_password(service, "refresh") is not None:
            backend.delete_password(service, "refresh")

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
            services = [self.service]
            if sys.platform == "win32":
                # Scan bounded slots even if the manifest was lost or malformed.
                services.extend(self._chunk_service(index) for index in range(self._max_chunks))
            failed = False
            for service in services:
                try:
                    self._delete_entry(backend, service)
                except Exception:
                    failed = True  # Still attempt every other entry.
            if failed:
                raise IdentityError("Credential deletion failed")
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
    # No TCP listener. The OS URL handler forwards the single-use code over native IPC.
    with pending_callback.waiting(state, logout=logout) as attempt:
        if cancelled.is_set():
            raise IdentityError("Sign-in was cancelled.")
        if not opener(url):
            raise IdentityError("Cannot open the system browser. Check your default browser.")
        deadline = time.monotonic() + timeout
        while not cancelled.is_set() and time.monotonic() < deadline:
            if attempt.received.wait(min(0.1, max(0, deadline - time.monotonic()))):
                break
        if cancelled.is_set() or not attempt.received.is_set() or not attempt.code:
            raise IdentityError("Sign-in was cancelled, rejected, or timed out.")
        return attempt.code


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
        self.tokens = tokens
        try:
            if self.cancelled.is_set():
                raise IdentityError("Sign-in was cancelled.")
            self.store.save(tokens.refresh, fresh=fresh)
            account = self.account()
            if self.cancelled.is_set():
                raise IdentityError("Sign-in was cancelled.")
            return account
        except IdentityError:
            with suppress(IdentityError):
                self.discard()
            raise

    def discard(self) -> str:
        """Invalidate an abandoned session without opening another browser flow."""
        refresh = self.tokens.refresh if self.tokens else self.pending_revocation
        self.tokens = None
        errors = []
        for operation in (self.store.block, self.store.delete):
            try:
                operation()
            except IdentityError as exc:
                errors.append(str(exc))
        if refresh:
            self.pending_revocation = refresh
            try:
                self.revoke(refresh)
                self.pending_revocation = None
            except IdentityError:
                errors.append("Online revocation failed. Retry sign-out when connected.")
        if errors:
            raise IdentityError(" ".join(errors))
        return "Sign-in was cancelled."

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
                    "logout_uri": SIGNOUT_CALLBACK,
                }
            ),
            "",
            self.cancelled,
            timeout=30,
            logout=True,
        )
        return "Signed out."
