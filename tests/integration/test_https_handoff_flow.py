"""Desktop -> API -> browser WebCrypto -> API -> desktop without native activation."""

import base64
import hashlib
import json
from functools import partial
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from backend.tests.api.test_browser_return import run_browser
from backend.tests.api.test_handoff import handoff as handoff
from fastapi.testclient import TestClient

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_desktop import auth, https_handoff
from buddy_desktop.native_callback import pending_callback
from buddy_identity.config import HANDOFF_PATH, IdentityConfig


def test_complete_http_handoff_without_os_protocol_or_loopback(handoff, monkeypatch):
    _, storage, cloud, _, _, body = handoff
    browser_query = {}
    provider_requests = []
    owner = str(uuid4())

    def provider(request, **kwargs):
        provider_requests.append(request)
        if request.full_url.endswith("/token"):
            return {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        return {"data": {"id": owner}}

    monkeypatch.setattr(auth, "request_json", provider)
    with TestClient(
        create_app(Settings(Environment.TEST, ("testserver",)), upload_storage=storage)
    ) as client:

        def request(request):
            response = client.post(
                urlsplit(request.full_url).path,
                content=request.data,
                headers=dict(request.header_items()),
            )
            assert response.status_code == 200
            return response.json()

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            browser_query.update(query)
            state = query["state"][0]
            key = client.post(HANDOFF_PATH + "/key", json={"state": state}).json()["public_key"]
            result = run_browser(
                "?" + urlencode({"state": state, "code": "one-use-browser-code"}), public_key=key
            )
            assert result["navigations"] == []
            for post in result["posts"]:
                response = client.post(
                    post["url"],
                    content=post["options"]["body"],
                    headers={"Content-Type": "application/json"},
                )
                assert response.status_code == 200
            return True

        monkeypatch.setattr(https_handoff, "request_json", request)
        monkeypatch.setattr(
            auth, "https_browser_code", partial(https_handoff.https_browser_code, opener=browser)
        )
        config = IdentityConfig(
            "eu-central-1",
            "eu-central-1_Example",
            "client123",
            "https://buddy.auth.eu-central-1.amazoncognito.com",
            "https://testserver/auth/desktop/callback",
        )
        store = Mock()
        session = auth.AccountSession(config, "https://testserver", store)
        assert session.sign_in() == owner
        exchange = parse_qs(provider_requests[0].data.decode())
        assert exchange["code"] == ["one-use-browser-code"]
        assert exchange["redirect_uri"] == browser_query["redirect_uri"] == [config.callback_url]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"][0].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert browser_query["code_challenge"] == [challenge]
        store.save.assert_called_once_with("synthetic-refresh", fresh=True)
        assert pending_callback.attempt is None
        assert b"one-use-browser-code" not in b"".join(cloud.versions)
        assert all(
            json.loads(content)["status"] == "consumed" for content, _ in cloud.objects.values()
        )
