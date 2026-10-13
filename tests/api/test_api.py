import asyncio
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import DatabaseProbe
from buddy_api.persistence.settings import DatabaseSettings


def app(**kwargs):
    return create_app(
        Settings(Environment.TEST, ("testserver", "localhost", "127.0.0.1")), **kwargs
    )


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/version", "/health"])
def test_live_service_contract_and_request_ids(path):
    with TestClient(app()) as client:
        response = client.get(path, headers={"X-Request-ID": "untrusted-input"})
    assert response.status_code == 200
    payload = response.json()
    assert str(UUID(payload["request_id"])) == response.headers["x-request-id"]
    assert payload["request_id"] != "untrusted-input"
    assert response.headers["cache-control"] == "no-store"
    if path != "/health":
        assert set(payload) == {"data", "request_id"}
    else:
        assert payload["status"] == "ok"


def test_readiness_is_not_reported_before_startup():
    response = TestClient(app()).get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json()["error"]["details"] == [
        {"location": "application", "code": "not_started"},
        {"location": "database", "code": "dependency_unavailable"},
    ]


def test_no_unconfigured_dependency_is_claimed_healthy():
    with TestClient(app()) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert response.json()["error"]["details"] == [
            {"location": "database", "code": "dependency_unavailable"}
        ]


def test_dependency_failure_and_recovery_redacts_details(monkeypatch):
    async def healthy_database(_probe):
        pass

    monkeypatch.setattr(DatabaseProbe, "check", healthy_database)

    class Probe:
        name = "upstream"
        failing = True

        async def check(self):
            if self.failing:
                raise ConnectionError("postgres://user:secret@private-db")

    probe = Probe()
    with TestClient(app(probes=[probe])) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert "secret" not in response.text
        assert response.json()["error"]["details"][0]["location"] == "upstream"
        probe.failing = False
        assert client.get("/api/v1/ready").json()["data"]["dependencies"] == [
            {"name": "database", "status": "ready"},
            {"name": "upstream", "status": "ready"},
        ]


def test_probe_timeout_does_not_break_liveness():
    class SlowProbe:
        name = "storage"

        async def check(self):
            await asyncio.sleep(60)

    application = create_app(
        Settings(Environment.TEST, ("testserver",), readiness_timeout=0.1), probes=[SlowProbe()]
    )
    with TestClient(application) as client:
        assert client.get("/api/v1/ready").status_code == 503
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost:999999", "user@localhost"])
def test_invalid_host_is_rejected_with_common_error_contract(host):
    with TestClient(app()) as client:
        response = client.get("/api/v1/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_validation_errors_do_not_echo_submitted_values():
    with TestClient(app()) as client:
        response = client.get("/api/v1/version", params={"api_version": "private-secret"})
    assert response.status_code == 422
    assert "private-secret" not in response.text
    assert response.json()["error"]["details"][0]["location"] == "query.api_version"


@pytest.mark.parametrize("method,path,code", [("GET", "/missing", 404), ("POST", "/health", 405)])
def test_routing_errors_have_consistent_contracts(method, path, code):
    with TestClient(app()) as client:
        response = client.request(method, path)
    assert response.status_code == code
    assert response.json()["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
def test_method_not_allowed_preserves_supported_methods(path):
    with TestClient(app()) as client:
        response = client.post(path)
    assert response.status_code == 405
    assert "GET" in {method.strip() for method in response.headers["allow"].split(",")}
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_http_error_preserves_protocol_headers_and_redacts_body():
    application = app()

    @application.get("/busy")
    async def busy():
        raise HTTPException(
            status_code=503,
            detail="private-secret",
            headers={
                "Retry-After": "10",
                "X-Request-ID": "untrusted-id",
                "Cache-Control": "public",
                "X-Content-Type-Options": "untrusted-value",
            },
        )

    with TestClient(application) as client:
        response = client.get("/busy")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert "private-secret" not in response.text
    assert response.json()["error"]["message"] == "Request rejected."
    assert str(UUID(response.headers["x-request-id"])) == response.json()["request_id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unknown_and_http_errors_are_redacted(caplog):
    application = app()

    @application.get("/unexpected")
    async def unexpected():
        raise ValueError("private-secret")

    @application.get("/denied")
    async def denied():
        raise HTTPException(status_code=403, detail="private-secret")

    with TestClient(application, raise_server_exceptions=False) as client:
        for path, status in [("/unexpected", 500), ("/denied", 403)]:
            response = client.get(path)
            assert response.status_code == status
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert "private-secret" not in response.text
    assert "private-secret" not in caplog.text


def test_production_docs_are_disabled():
    settings = Settings(
        Environment.PRODUCTION,
        ("testserver",),
        database=DatabaseSettings(
            "postgresql://buddy:synthetic@localhost/buddy?sslmode=verify-full", production=True
        ),
    )
    with TestClient(create_app(settings)) as client:
        for path in ["/docs", "/openapi.json"]:
            assert client.get(path).status_code == 404


def test_duplicate_probe_names_fail_at_startup():
    class Probe:
        name = "database"

        async def check(self):
            pass

    with pytest.raises(ValueError, match="unique"):
        app(probes=[Probe(), Probe()])
