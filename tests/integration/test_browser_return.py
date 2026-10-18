"""A real finish-page script feeds the native broker and preserves the PKCE exchange."""

from functools import partial
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from backend.tests.api.test_browser_return import run_browser
from fastapi.testclient import TestClient

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_desktop import auth
from buddy_desktop.native_callback import pending_callback
from buddy_identity.config import IdentityConfig


def test_browser_page_native_broker_and_pkce_exchange_use_the_same_redirect(monkeypatch):
    config = IdentityConfig(
        "eu-central-1",
        "eu-central-1_Example",
        "client123",
        "https://buddy.auth.eu-central-1.amazoncognito.com",
        "https://api.example.test/auth/desktop/callback",
    )
    owner = str(uuid4())
    calls = []

    def provider(request, **kwargs):
        calls.append(request)
        if request.full_url.endswith("/token"):
            return {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        return {"data": {"id": owner}}

    monkeypatch.setattr(auth, "request_json", provider)
    with TestClient(create_app(Settings(Environment.TEST, ("testserver",)))) as api:

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            assert query["redirect_uri"] == [config.callback_url]
            callback = "?" + urlencode({"state": query["state"][0], "code": "synthetic-code"})
            page = api.get("/auth/desktop/callback" + callback)
            assert page.status_code == 200 and "synthetic-code" not in page.text
            native = run_browser(callback)["navigations"][0]
            assert pending_callback.deliver(native)
            assert not pending_callback.deliver(native)  # Same callback cannot finish twice.
            return True

        monkeypatch.setattr(auth, "browser_code", partial(auth.browser_code, opener=browser))
        store = Mock()
        client = auth.AccountSession(config, "https://api.example.test", store)
        assert client.sign_in() == owner
        exchange = parse_qs(calls[0].data.decode())
        assert exchange["redirect_uri"] == [config.callback_url]
        assert exchange["code"] == ["synthetic-code"]
        assert len(exchange["code_verifier"][0]) >= 43
        store.save.assert_called_once_with("synthetic-refresh", fresh=True)
        assert pending_callback.attempt is None
