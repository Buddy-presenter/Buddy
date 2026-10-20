"""CloudWatch metric events stay useful without capturing presentation data."""

import asyncio
import json
from uuid import uuid4

import pytest

from buddy_api.middleware import RequestContextMiddleware
from buddy_api.observability import emit_metric


def test_metric_event_has_only_bounded_dimensions_and_opaque_correlation(capsys):
    identifier = str(uuid4())
    emit_metric(
        environment="staging",
        service="worker",
        stage="provider",
        outcome="success",
        metric="InputTokens",
        value=1234,
        correlation_ids={"job_id": identifier, "deck_id": identifier},
        analysis_version=3,
    )

    line = capsys.readouterr().out
    assert line.startswith("{") and line.rstrip().endswith("}")
    document = json.loads(line)
    assert document["InputTokens"] == 1234
    assert document["job_id"] == identifier
    assert document["deck_id"] == identifier
    assert document["analysis_version"] == 3
    assert document["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
        ["Environment", "Service", "Stage", "Outcome"],
        ["Environment", "Service", "Outcome"],
        ["Environment", "Service"],
    ]
    assert "summary" not in document and "filename" not in document


def test_metric_rejects_unbounded_or_private_dimensions(capsys):
    emit_metric(
        environment="staging",
        service="worker",
        stage="presentation-name.pdf",
        outcome="success",
        metric="JobOutcome",
        correlation_ids={"deck_id": "presentation-name.pdf"},
    )

    assert capsys.readouterr().out == ""


def test_metric_rejects_non_finite_and_negative_values(capsys):
    for value in (-1, float("nan"), float("inf")):
        emit_metric(
            environment="test",
            service="api",
            stage="upload",
            outcome="success",
            metric="DurationMs",
            value=value,
        )

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("status", "expected"), [(201, "success"), (413, "rejected")])
def test_upload_middleware_emits_success_and_rejection_without_path_identifiers(
    capsys, status, expected
):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    middleware = RequestContextMiddleware(app, ("buddy.test",))
    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/api/v1/uploads/{uuid4()}/content",
        "query_string": b"",
        "headers": [(b"host", b"buddy.test")],
        "state": {},
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))

    assert [
        message["status"] for message in messages if message["type"] == "http.response.start"
    ] == [status]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2
    assert {event["Outcome"] for event in events} == {expected}
    assert all(event["Stage"] == "upload" and event["Service"] == "api" for event in events)
    assert all("content" not in json.dumps(event) for event in events)
