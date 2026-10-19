"""The running desktop alone can decrypt its browser return; no native activation."""

import base64
import hashlib
import json
import secrets
from threading import Event

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from buddy_desktop import https_handoff
from buddy_identity.config import IdentityError

STATE = "buddy-v1-" + "s" * 43
URL = "https://pool.example.test/oauth2/authorize?redirect_uri=https%3A%2F%2Fapi.example.test%2Fauth%2Fdesktop%2Fcallback"


def seal(public_key, state=STATE, code="one-use-code", *, tamper=False):
    public = serialization.load_der_public_key(base64.b64decode(public_key))
    key, nonce = AESGCM.generate_key(256), secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps({"state": state, "code": code}).encode(), STATE.encode()
    )
    if tamper:
        ciphertext = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
    return {
        "key": base64.b64encode(
            public.encrypt(
                key,
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
                ),
            )
        ).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "data": base64.b64encode(ciphertext).decode(),
    }


def test_https_collection_is_encrypted_pinned_to_origin_and_private_capability(monkeypatch):
    requests = []
    start = {}

    def respond(request):
        requests.append(request)
        body = json.loads(request.data)
        if request.full_url.endswith("/start"):
            start.update(body)
            return {"status": "pending"}
        if body.get("action") == "cancel":
            return {"status": "consumed"}
        secret = request.get_header("Authorization").split(" ")[1]
        assert start["read_hash"] == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in URL and secret not in repr(start)
        return {"status": "complete", "sealed": seal(start["public_key"])}

    monkeypatch.setattr(https_handoff, "request_json", respond)
    assert (
        https_handoff.https_browser_code(URL, STATE, Event(), opener=lambda _: True)
        == "one-use-code"
    )
    assert all(
        request.full_url.startswith("https://api.example.test/auth/desktop/handoff/v1/")
        for request in requests
    )


@pytest.mark.parametrize(
    "failure", ["tamper", "wrong-state", "empty-code", "token-result", "malformed", "expired"]
)
def test_invalid_or_unavailable_results_never_become_authentication(monkeypatch, failure):
    start = {}

    def respond(request):
        body = json.loads(request.data)
        if request.full_url.endswith("/start"):
            start.update(body)
            return {"status": "pending"}
        if body.get("action") == "cancel":
            return {}
        if failure == "expired":
            return {"status": "expired"}
        if failure == "malformed":
            return {"status": "complete", "sealed": {"data": "private-malformed"}}
        return {
            "status": "complete",
            "sealed": seal(
                start["public_key"],
                state="other" if failure == "wrong-state" else STATE,
                code=""
                if failure == "empty-code"
                else {"access_token": "private"}
                if failure == "token-result"
                else "code",
                tamper=failure == "tamper",
            ),
        }

    monkeypatch.setattr(https_handoff, "request_json", respond)
    with pytest.raises(IdentityError) as error:
        https_handoff.https_browser_code(URL, STATE, Event(), opener=lambda _: True)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "finish",
    [
        "cancel-before",
        "cancel-after-start",
        "cancel-in-browser",
        "browser-failure",
        "timeout",
        "start-failure",
    ],
)
def test_cancellation_browser_failure_expiry_and_registration_failure(monkeypatch, finish):
    event = Event()
    requests = []
    if finish == "cancel-before":
        event.set()

    def respond(request):
        requests.append(json.loads(request.data))
        if request.full_url.endswith("/start"):
            if finish == "cancel-after-start":
                event.set()
            if finish == "start-failure":
                raise IdentityError("Secure handoff unavailable.")
        return {"status": "pending"}

    def opener(_url):
        if finish == "cancel-in-browser":
            event.set()
        return finish != "browser-failure"

    monkeypatch.setattr(https_handoff, "request_json", respond)
    with pytest.raises(IdentityError):
        https_handoff.https_browser_code(URL, STATE, event, opener=opener, timeout=0)
    if finish == "cancel-before":
        assert requests == []
    elif finish != "start-failure":
        assert requests[-1]["action"] == "cancel"


def test_lost_collection_response_requires_a_fresh_attempt_and_does_not_reopen_browser(monkeypatch):
    requests = []
    opened = []

    def respond(request):
        requests.append(request)
        if request.full_url.endswith("/start"):
            return {"status": "pending"}
        raise IdentityError("Account service unavailable.")

    monkeypatch.setattr(https_handoff, "request_json", respond)
    with pytest.raises(IdentityError, match="fresh sign-in"):
        https_handoff.https_browser_code(
            URL, STATE, Event(), opener=lambda url: opened.append(url) or True
        )
    assert opened == [URL]
    assert json.loads(requests[-1].data)["action"] == "cancel"
