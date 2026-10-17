"""OAuth boundaries and restart/sign-out safety without a live identity provider or OS vault."""

import hashlib
import time
from threading import Event
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from buddy_desktop import auth
from buddy_identity.config import IdentityConfig, IdentityError, SessionExpired

CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_Example",
    "client123",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


class Store:
    def __init__(self):
        self.token = None
        self.blocked = False

    def read(self):
        return None if self.blocked else self.token

    def save(self, token, *, fresh=False):
        self.token = token
        if fresh:
            self.blocked = False

    def block(self):
        self.blocked = True

    def delete(self):
        self.token = None


@pytest.fixture
def session(monkeypatch):
    store = Store()
    session = auth.AccountSession(CONFIG, "https://api.example.com", store)
    requests = []
    identifier = str(uuid4())

    def respond(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/token"):
            return {
                "access_token": "access-private",
                "refresh_token": "refresh-private",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        if request.full_url.endswith("/me"):
            assert request.get_header("Authorization") == "Bearer access-private"
            return {"data": {"id": identifier}}
        return {}

    monkeypatch.setattr(auth, "request_json", respond)
    monkeypatch.setattr(auth, "browser_code", lambda *args, **kwargs: "one-time-code")
    return session, requests, identifier


@pytest.mark.parametrize("action", ["signin", "signup", "recovery"])
def test_supported_browser_flows_use_pkce_and_no_tokens_in_urls(session, monkeypatch, action):
    client, requests, identifier = session
    urls = []
    monkeypatch.setattr(auth, "browser_code", lambda url, *args: urls.append(url) or "code")
    assert client.sign_in(action) == identifier
    parameters = parse_qs(urlsplit(urls[0]).query)
    assert (
        urlsplit(urls[0]).path
        == {
            "signin": "/oauth2/authorize",
            "signup": "/signup",
            "recovery": "/forgotPassword",
        }[action]
    )
    assert parameters["redirect_uri"] == ["org.buddy.desktop://oauth/callback"]
    assert parameters["prompt"] == ["login"]
    exchange = parse_qs(requests[0].data.decode())
    expected = (
        auth.base64.urlsafe_b64encode(
            hashlib.sha256(exchange["code_verifier"][0].encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    assert parameters["code_challenge"] == [expected]
    assert parameters["response_type"] == ["code"]
    assert client.store.token == "refresh-private"
    assert all("private" not in r.full_url for r in requests)
    assert "private" not in repr(client.tokens)


def test_restart_and_expiration_rotate_saved_token(session):
    client, requests, identifier = session
    client.sign_in()
    restored = auth.AccountSession(CONFIG, client.api_url, client.store)
    assert restored.resume() == identifier
    assert parse_qs(requests[-2].data.decode())["grant_type"] == ["refresh_token"]
    restored.tokens.expires = time.monotonic()
    assert restored.account() == identifier


def test_invalid_refresh_blocks_restore(session, monkeypatch):
    client, _, _ = session
    client.store.save("expired")
    monkeypatch.setattr(
        client, "token_request", lambda _: (_ for _ in ()).throw(SessionExpired("expired"))
    )
    with pytest.raises(SessionExpired):
        client.resume()
    assert client.store.read() is None
    assert client.tokens is None


def test_vault_write_failure_revokes_new_session(session, monkeypatch):
    client, requests, _ = session
    monkeypatch.setattr(
        client.store,
        "save",
        lambda *a, **k: (_ for _ in ()).throw(IdentityError("vault unavailable")),
    )
    with pytest.raises(IdentityError, match="vault"):
        client.sign_in()
    assert requests[-1].full_url.endswith("/revoke")
    assert client.tokens is None


def test_signout_clears_vault_and_revokes(session):
    client, requests, _ = session
    client.sign_in()
    assert client.sign_out() == "Signed out."
    assert requests[-1].full_url.endswith("/revoke")
    assert client.store.read() is None
    assert client.tokens is None


def test_signout_network_failure_can_be_retried(session, monkeypatch):
    client, _, _ = session
    client.sign_in()
    monkeypatch.setattr(client, "revoke", lambda _: (_ for _ in ()).throw(IdentityError("offline")))
    with pytest.raises(IdentityError, match="revocation failed"):
        client.sign_out()
    assert client.store.read() is None
    assert client.tokens is None
    revoked = []
    monkeypatch.setattr(client, "revoke", revoked.append)
    client.sign_out()
    assert revoked == ["refresh-private"]


def test_native_store_deletion_failure_leaves_nonsecret_restart_block(tmp_path, monkeypatch):
    store = auth.NativeTokenStore("test-account", tmp_path)

    class Vault:
        def get_password(self, *args):
            return "sensitive-refresh"

        def delete_password(self, *args):
            raise RuntimeError("sensitive-refresh")

    monkeypatch.setattr(store, "backend", Vault)
    store.block()
    with pytest.raises(IdentityError) as error:
        store.delete()
    assert "sensitive-refresh" not in str(error.value)
    assert store.read() is None
    assert "sensitive" not in store.marker.read_text()


def test_native_callback_rejects_wrong_state_and_accepts_single_use_code():
    from buddy_desktop.native_callback import pending_callback
    from buddy_identity.config import CALLBACK

    def open_browser(_url):
        assert not pending_callback.deliver(CALLBACK + "?state=wrong&code=untrusted")
        assert pending_callback.deliver(CALLBACK + "?state=expected&code=valid")
        assert not pending_callback.deliver(CALLBACK + "?state=expected&code=replay")
        return True

    assert (
        auth.browser_code(
            "https://example.com", "expected", Event(), timeout=1, opener=open_browser
        )
        == "valid"
    )
    assert pending_callback.attempt is None
    assert not pending_callback.deliver(CALLBACK + "?state=expected&code=late")


def test_callback_cancellation_is_bounded():
    event = Event()
    event.set()
    with pytest.raises(IdentityError, match="cancelled"):
        auth.browser_code("https://example.com", "state", event, timeout=1, opener=lambda _: True)


def test_rejected_browser_recovery_never_creates_a_session():
    from buddy_desktop.native_callback import pending_callback
    from buddy_identity.config import CALLBACK

    def open_browser(_url):
        assert pending_callback.deliver(CALLBACK + "?state=expected&error=access_denied")
        return True

    with pytest.raises(IdentityError, match="rejected"):
        auth.browser_code(
            "https://example.com", "expected", Event(), timeout=1, opener=open_browser
        )


@pytest.mark.parametrize(
    "field,value",
    [("domain", "http://evil.example"), ("client_id", "secret?"), ("pool_id", "us-east-1_Wrong")],
)
def test_invalid_identity_configuration(field, value):
    values = {
        "region": CONFIG.region,
        "pool_id": CONFIG.pool_id,
        "client_id": CONFIG.client_id,
        "domain": CONFIG.domain,
    }
    values[field] = value
    with pytest.raises(IdentityError):
        IdentityConfig(**values)


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("boundary", ["token", "me", "queued_result"])
def test_widget_late_cancellation_clears_and_revokes_session(
    session, monkeypatch, qtbot, shutdown, boundary
):
    from buddy_desktop.account_widget import AccountWidget

    client, requests, _ = session
    widget = AccountWidget(client.api_url)
    qtbot.addWidget(widget)
    widget.session = client
    entered, release = Event(), Event()
    respond = auth.request_json

    def blocked(request, **kwargs):
        if request.full_url.endswith("/" + boundary):
            entered.set()
            assert release.wait(5), "Test did not release the provider response"
        return respond(request, **kwargs)

    monkeypatch.setattr(auth, "request_json", blocked)
    widget.start("signin")
    try:
        if boundary == "queued_result":
            # Complete the worker without dispatching its queued success on the GUI thread.
            assert widget.runner.pool.waitForDone(5000)
            assert client.tokens is not None
        else:
            qtbot.waitUntil(entered.is_set, timeout=5000)
        if shutdown:
            widget.shutdown()
        else:
            widget.cancel.click()
        release.set()
        qtbot.waitUntil(lambda: not widget.runner.busy, timeout=5000)
        assert client.tokens is None
        assert client.store.read() is None
        assert client.store.token is None
        assert any(request.full_url.endswith("/revoke") for request in requests)
        assert widget.message.text() != "Signed in to Buddy."
        assert not widget.timer.isActive()
    finally:
        release.set()
        widget.runner.wait_for_shutdown()
