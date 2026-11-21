"""HTTP adapter only; never supplies a client-side verdict or provider credential."""

import json
from threading import Event
from uuid import uuid4

import pytest

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.preflight_service import PreflightService
from buddy_desktop.services import ServiceError


def test_request_only_selects_saved_action_and_checks_result_identity(backend):
    analysis = uuid4()
    report = dict(
        id=str(uuid4()),
        analysis_id=str(analysis),
        instance_id="action",
        state="validated",
        phase="complete",
        reasons=[],
    )
    backend.body = json.dumps(dict(data=report)).encode()
    service = PreflightService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5), "access")
    assert (
        service.run(
            analysis,
            "action",
            profile="wide-1x",
            viewport=(1280, 720),
            correct=True,
            cancelled=Event(),
        )
        == report
    )
    assert backend.authorization == "Bearer access"
    body = json.loads(backend.request_body)
    assert set(body) == {"instance_id", "profile", "viewport", "runtime_platform", "correct"}
    assert backend.path == f"/api/v1/analyses/{analysis}/preflights"
    backend.body = json.dumps(dict(data={**report, "analysis_id": str(uuid4())})).encode()
    with pytest.raises(ServiceError, match="another action"):
        service.run(
            analysis,
            "action",
            profile="wide-1x",
            viewport=(1280, 720),
            correct=True,
            cancelled=Event(),
        )


@pytest.mark.parametrize(
    "status,exception",
    [
        (401, LibraryAccessExpired),
        (404, ServiceError),
        (409, ServiceError),
        (429, ServiceError),
        (503, ServiceError),
    ],
)
def test_safe_preflight_failure_messages(backend, status, exception):
    backend.response_code = status
    backend.body = b'{"private":"do not echo tokens, provider errors, or paths"}'
    service = PreflightService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5), "access")
    with pytest.raises(exception) as error:
        service.request("preflights/" + str(uuid4()))
    assert "private" not in str(error.value) and "tokens" not in str(error.value)


def test_pending_check_can_stop_waiting_without_claiming_a_verdict(backend):
    report = dict(id=str(uuid4()), phase="running", state="pending")
    backend.body = json.dumps(dict(data=report)).encode()
    service = PreflightService(AppConfig(f"http://127.0.0.1:{backend.server_port}", 0.5), "access")
    cancelled = Event()
    cancelled.set()
    with pytest.raises(ServiceError, match="finish and remain saved"):
        service.run(
            uuid4(),
            "action",
            profile="wide-1x",
            viewport=(1280, 720),
            correct=True,
            cancelled=cancelled,
        )
