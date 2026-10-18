"""Verified account fields come from the same live provider lookup as authentication."""

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from buddy_api import auth
from buddy_api.config import Environment, Settings
from buddy_identity.config import IdentityConfig, IdentityError

pytestmark = pytest.mark.postgres
CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_Example",
    "client123",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


def test_profile_is_provider_verified_current_and_scoped_to_bearer(
    migrated_database, api_client_factory, monkeypatch
):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = auth.CognitoVerifier(CONFIG)
    monkeypatch.setattr(
        verifier.keys,
        "get_signing_key_from_jwt",
        lambda _: SimpleNamespace(key=private.public_key()),
    )
    subjects = [str(uuid4()), str(uuid4())]
    emails = {subjects[0]: "first@example.test", subjects[1]: "second@example.test"}
    tokens = [
        jwt.encode(
            {
                "sub": sub,
                "iss": CONFIG.issuer,
                "client_id": CONFIG.client_id,
                "token_use": "access",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "email": "forged@example.test",
                "email_verified": True,
            },
            private,
            algorithm="RS256",
        )
        for sub in subjects
    ]
    calls = []

    def provider(request):
        calls.append(request)
        token = json.loads(request.data)["AccessToken"]
        sub = subjects[tokens.index(token)]
        return {
            "UserAttributes": [
                {"Name": "sub", "Value": sub},
                {"Name": "email", "Value": emails[sub]},
                {"Name": "email_verified", "Value": "true"},
            ]
        }

    monkeypatch.setattr(auth, "request_json", provider)
    client = api_client_factory(
        Settings(Environment.TEST, ("testserver",), database=migrated_database, identity=CONFIG)
    )
    client.app.state.authentication.verifier = verifier
    first = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tokens[0]})
    second = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tokens[1]})
    a, b = first.json()["data"], second.json()["data"]
    assert a["id"] != b["id"]
    assert a["email"] == emails[subjects[0]] and b["email"] == emails[subjects[1]]
    assert a["email_verified"] is b["email_verified"] is True
    assert set(a) == {"id", "email", "email_verified"}
    assert "forged@example.test" not in first.text
    assert first.headers["cache-control"] == "no-store"
    scoped = client.get(
        "/api/v1/auth/me?account_id=" + b["id"],
        headers={"Authorization": "Bearer " + tokens[0], "X-Account-Id": b["id"]},
    )
    assert scoped.json()["data"] == a
    emails[subjects[0]] = "updated@example.test"
    updated = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tokens[0]})
    assert updated.json()["data"] == {**a, "email": "updated@example.test"}
    assert len(calls) == 4  # One uncached GetUser call per request, not a separate profile lookup.
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.get(
            "/api/v1/auth/me/" + b["id"], headers={"Authorization": "Bearer " + tokens[0]}
        ).status_code
        == 404
    )
    monkeypatch.setattr(
        auth, "request_json", lambda _: (_ for _ in ()).throw(IdentityError("offline"))
    )
    unavailable = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + tokens[0]})
    assert unavailable.status_code == 503 and "example.test" not in unavailable.text
