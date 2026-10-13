import pytest

from buddy_desktop.config import AppConfig
from buddy_desktop.services import MAX_RESPONSE_BYTES, HealthService, ServiceError


def service(backend):
    return HealthService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.2))


def test_health_check_uses_real_http(backend):
    assert service(backend).check().status == "ok"


@pytest.mark.parametrize(
    ("code", "body", "message"),
    [
        (404, b"private backend detail", "not found"),
        (403, b"private backend detail", "denied access"),
        (503, b"private backend detail", "HTTP 503"),
        (200, b"not json", "invalid health response"),
        (200, b'{"status":"degraded"}', "healthy status"),
        (200, b"[]", "healthy status"),
        (302, b"", "HTTP 302"),
    ],
)
def test_backend_failures_are_recoverable_and_do_not_expose_body(backend, code, body, message):
    backend.response_code = code
    backend.body = body
    with pytest.raises(ServiceError, match=message) as failure:
        service(backend).check()
    assert "private" not in str(failure.value)


def test_oversized_response_rejected(backend):
    backend.body = b" " * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ServiceError, match="exceeds"):
        service(backend).check()


def test_non_json_content_type_rejected(backend):
    backend.content_type = "text/html"
    with pytest.raises(ServiceError, match="unsupported"):
        service(backend).check()


def test_connection_timeout_is_recoverable(backend):
    backend.release.clear()
    with pytest.raises(ServiceError, match="Cannot reach"):
        service(backend).check()
