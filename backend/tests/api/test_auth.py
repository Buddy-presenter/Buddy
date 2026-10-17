"""Actual signed JWT validation, online revocation and bounded public endpoints."""

import time
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from buddy_api import auth
from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_identity.config import IdentityConfig, SessionExpired

CONFIG = IdentityConfig(
    "eu-central-1",
    "eu-central-1_Example",
    "client123",
    "https://buddy.auth.eu-central-1.amazoncognito.com",
)


@pytest.fixture
def signed(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = auth.CognitoVerifier(CONFIG)
    monkeypatch.setattr(
        verifier.keys,
        "get_signing_key_from_jwt",
        lambda _: SimpleNamespace(key=private.public_key()),
    )
    claims = {
        "sub": str(uuid4()),
        "iss": CONFIG.issuer,
        "client_id": CONFIG.client_id,
        "token_use": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    monkeypatch.setattr(
        auth,
        "request_json",
        lambda _: {
            "UserAttributes": [
                {"Name": "sub", "Value": claims["sub"]},
                {"Name": "email", "Value": "verified@example.test"},
                {"Name": "email_verified", "Value": "true"},
            ]
        },
    )
    return verifier, claims, private


def test_signed_account_identity_and_revocation(signed, monkeypatch):
    verifier, claims, key = signed
    token = jwt.encode(claims, key, algorithm="RS256")
    assert verifier.verify(token) == CONFIG.issuer + "#" + claims["sub"]
    monkeypatch.setattr(
        auth, "request_json", lambda _: (_ for _ in ()).throw(SessionExpired("revoked"))
    )
    with pytest.raises(SessionExpired):
        verifier.verify(token)


@pytest.mark.parametrize(
    "change",
    [
        {"exp": 1},
        {"iss": "https://other.example"},
        {"client_id": "wrong"},
        {"token_use": "id"},
        {"sub": ""},
    ],
)
def test_invalid_token_claims_rejected(signed, change):
    verifier, claims, key = signed
    with pytest.raises(SessionExpired):
        verifier.verify(jwt.encode({**claims, **change}, key, algorithm="RS256"))


def test_wrong_signature_and_unsigned_tokens_rejected(signed):
    verifier, claims, _ = signed
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in [
        jwt.encode(claims, other, algorithm="RS256"),
        jwt.encode(claims, "", algorithm="none"),
    ]:
        with pytest.raises(SessionExpired):
            verifier.verify(token)


def test_unconfigured_account_and_rate_limit_are_safe():
    with TestClient(create_app(Settings(Environment.TEST, ("testserver",)))) as client:
        for _ in range(30):
            assert client.get("/api/v1/auth/me").status_code == 503
        result = client.get("/api/v1/auth/me")
        assert result.status_code == 429
        assert result.headers["Retry-After"] == "60"
        assert result.headers["Cache-Control"] == "no-store"


def test_malformed_authorization_is_not_echoed():
    app = create_app(Settings(Environment.TEST, ("testserver",), identity=CONFIG))
    with TestClient(app) as client:
        app.state.database = SimpleNamespace()
        result = client.get("/api/v1/auth/me", headers={"Authorization": "secret-private"})
        assert result.status_code == 401
        assert "secret-private" not in result.text
        app.state.database = None


@pytest.mark.parametrize(
    "attributes",
    [
        None,
        {},
        [],
        [{"Name": "email_verified", "Value": "false"}],
        [{"Name": "email_verified", "Value": True}],
        [{"Name": "email_verified", "Value": "TRUE"}],
        [{"Name": "email_verified", "Value": "true"}],
        [{"Name": "email_verified", "Value": "true"}, {"Name": "email", "Value": "invalid"}],
        [
            {"Name": "email_verified", "Value": "true"},
            {"Name": "email", "Value": "a@example.test"},
            {"Name": "email_verified", "Value": "false"},
        ],
    ],
)
def test_missing_unverified_or_ambiguous_provider_email_rejected(signed, monkeypatch, attributes):
    verifier, claims, key = signed
    if isinstance(attributes, list):
        attributes = [{"Name": "sub", "Value": claims["sub"]}, *attributes]
    monkeypatch.setattr(auth, "request_json", lambda _: {"UserAttributes": attributes})
    # Client/token-supplied verified claims cannot override GetUser's live attributes.
    token = jwt.encode(
        {**claims, "email_verified": True, "email": "a@example.test"}, key, algorithm="RS256"
    )
    with pytest.raises(SessionExpired):
        verifier.verify(token)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/decks"),
        ("GET", "/api/v1/analyses/00000000-0000-4000-8000-000000000001"),
        ("GET", "/api/v1/resources/decks/00000000-0000-4000-8000-000000000001"),
        ("GET", "/api/v1/sessions/00000000-0000-4000-8000-000000000001/events"),
        ("GET", "/api/v1/uploads/00000000-0000-4000-8000-000000000001"),
        ("PUT", "/api/v1/uploads/00000000-0000-4000-8000-000000000001/content"),
        ("POST", "/api/v1/uploads"),
        ("PATCH", "/api/v1/resources/decks/00000000-0000-4000-8000-000000000001"),
        ("DELETE", "/api/v1/resources/decks/00000000-0000-4000-8000-000000000001"),
    ],
)
def test_unverified_identity_cannot_bypass_any_private_api(signed, monkeypatch, method, path):
    verifier, claims, key = signed
    monkeypatch.setattr(
        auth,
        "request_json",
        lambda _: {
            "UserAttributes": [
                {"Name": "sub", "Value": claims["sub"]},
                {"Name": "email", "Value": "unverified@example.test"},
                {"Name": "email_verified", "Value": "false"},
            ]
        },
    )
    token = jwt.encode(claims, key, algorithm="RS256")
    app = create_app(Settings(Environment.TEST, ("testserver",), identity=CONFIG))
    with TestClient(app) as client:
        app.state.authentication.verifier = verifier
        # Any DB call would fail: rejection must happen before account bootstrap/resource access.
        app.state.database = SimpleNamespace()
        try:
            response = client.request(method, path, headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401
            assert "unverified@example.test" not in response.text
        finally:
            app.state.database = None
