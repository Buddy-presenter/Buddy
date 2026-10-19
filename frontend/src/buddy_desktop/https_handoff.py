"""Collect an encrypted browser return without an OS app link or local listener."""

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from threading import Event
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from buddy_identity.config import HANDOFF_PATH, IdentityError
from buddy_identity.transport import request_json


def https_browser_code(
    url: str,
    state: str,
    cancelled: Event,
    *,
    timeout: float = 300,
    opener: Callable[[str], bool] = webbrowser.open,
) -> str:
    # The public redirect URI is fixed/allowlisted; the collection capability never
    # enters the browser URL. RSA private material exists only for this invocation.
    from urllib.parse import parse_qs

    callback = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0])
    endpoint = f"{callback.scheme}://{callback.netloc}{HANDOFF_PATH}"
    secret = secrets.token_urlsafe(32)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def request(
        action: str, body: dict[str, Any], *, authenticated: bool = False
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Buddy-Handoff " + secret
        return request_json(
            Request(endpoint + "/" + action, data=json.dumps(body).encode(), headers=headers)
        )

    if cancelled.is_set():
        raise IdentityError("Sign-in was cancelled.")
    registered = False
    try:
        response = request(
            "start",
            {
                "state": state,
                "read_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "public_key": base64.b64encode(
                    private.public_key().public_bytes(
                        serialization.Encoding.DER,
                        serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                ).decode(),
            },
        )
        if response != {"status": "pending"}:
            raise IdentityError("Secure sign-in handoff is unavailable. Please try again.")
        registered = True
        if cancelled.is_set():
            raise IdentityError("Sign-in was cancelled.")
        if not opener(url):
            raise IdentityError("Cannot open the system browser. Check your default browser.")
        deadline = time.monotonic() + timeout
        while not cancelled.is_set() and time.monotonic() < deadline:
            result = request("poll", {"state": state}, authenticated=True)
            if result.get("status") == "complete":
                registered = False  # The API atomically consumed this attempt.
                try:
                    sealed = result["sealed"]
                    key = private.decrypt(
                        base64.b64decode(sealed["key"], validate=True),
                        padding.OAEP(
                            mgf=padding.MGF1(hashes.SHA256()),
                            algorithm=hashes.SHA256(),
                            label=None,
                        ),
                    )
                    if (
                        len(key) != 32
                        or len(base64.b64decode(sealed["nonce"], validate=True)) != 12
                    ):
                        raise ValueError
                    plaintext = AESGCM(key).decrypt(
                        base64.b64decode(sealed["nonce"], validate=True),
                        base64.b64decode(sealed["data"], validate=True),
                        state.encode(),
                    )
                    value = json.loads(plaintext)
                    if not isinstance(value, dict) or value.get("state") != state:
                        raise ValueError
                    if set(value) != {"state", "code"} or not isinstance(value["code"], str):
                        raise ValueError
                    code = value["code"]
                    if not 1 <= len(code) <= 4096 or any(
                        ord(c) < 33 or ord(c) == 127 for c in code
                    ):
                        raise ValueError
                except Exception:
                    raise IdentityError(
                        "Sign-in was rejected or the return was invalid. Please try again."
                    ) from None
                if cancelled.is_set():
                    raise IdentityError("Sign-in was cancelled.")
                return code
            if result != {"status": "pending"}:
                raise IdentityError("Sign-in return expired. Start a fresh sign-in from Buddy.")
            cancelled.wait(min(2, max(0, deadline - time.monotonic())))
        if cancelled.is_set():
            raise IdentityError("Sign-in was cancelled.")
        raise IdentityError(
            "Buddy did not receive the browser result in time. Start a fresh sign-in."
        )
    finally:
        if registered:
            with suppress(IdentityError):
                request("poll", {"state": state, "action": "cancel"}, authenticated=True)
