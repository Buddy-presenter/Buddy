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


@pytest.mark.parametrize("outcome", ["validated", "rejected", "failed"])
def test_desktop_check_sends_screenshots_and_never_decides(monkeypatch, outcome):
    """PowerPoint: this PC captures; the server judges each attempt and binds the result."""
    import base64

    import buddy_desktop.desktop_capture as capture
    from buddy_desktop.desktop_capture import CaptureResult

    analysis, check = uuid4(), str(uuid4())
    service = PreflightService(AppConfig("http://127.0.0.1:9", 0.5), "access")
    posts = []
    snapshot = dict(source_format="pptx", source_sha256="s" * 64, action={"instance_id": "a"})
    monkeypatch.setattr(
        service,
        "request",
        lambda path, body=None: dict(id=check, state="pending", phase="queued"),
    )

    def post(path, body, *, limit=65536):
        posts.append((path, body))
        if path.endswith("/claim"):
            return snapshot
        if path.endswith("/judge"):
            return {"verdict": "validated", "server": True}
        return dict(id=check, instance_id="a", state=outcome, phase="complete")

    monkeypatch.setattr(service, "_post", post)
    verdicts = []

    def fake_capture(received, source, judge, cancelled, progress):
        assert received is snapshot and source == b"pptx-bytes"
        progress(1)
        verdicts.append(judge(1, {"purpose": "x"}, b"\x89PNGbefore", b"\x89PNGafter"))
        if outcome == "failed":
            return CaptureResult(None, {}, "audience_viewport_occluded")
        return CaptureResult(
            dict(state=outcome, history=[{"version": 0, "state": outcome}]),
            {"attempt-0-before_image": b"\x89PNGbefore", "attempt-0-after_image": b"\x89PNGafter"},
        )

    monkeypatch.setattr(capture, "capture_check", fake_capture)
    steps = []
    result = service.run_on_desktop(
        analysis,
        "a",
        b"pptx-bytes",
        profile="wide-2x",
        viewport=(2560, 1600),
        cancelled=Event(),
        progress=steps.append,
    )
    assert result["state"] == outcome and steps == [1]
    # The verdict the player saw is exactly what the server returned.
    assert verdicts == [{"verdict": "validated", "server": True}]
    paths = [path.rsplit("/", 1)[-1] for path, _ in posts]
    assert paths == ["claim", "judge", "fail" if outcome == "failed" else "result"]
    judge_body = posts[1][1]
    assert base64.b64decode(judge_body["before"]) == b"\x89PNGbefore"
    if outcome == "failed":
        assert posts[2][1] == {"reason": "audience_viewport_occluded"}
    else:
        sent = posts[2][1]
        assert sent["report"]["state"] == outcome
        assert set(sent["images"]) == {"attempt-0-before_image", "attempt-0-after_image"}


def test_desktop_capture_runs_only_powerpoint_on_windows():
    from buddy_desktop.desktop_capture import capture_check

    result = capture_check(
        {"source_format": "pdf", "source_sha256": "x"}, b"", lambda *_: None, Event()
    )
    assert result.report is None and result.failure == "powerpoint_requires_windows_office"


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
