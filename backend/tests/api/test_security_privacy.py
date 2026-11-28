"""Exercise private failure paths and provider resource boundaries without paid calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from buddy_api.agent_provider import AgentFailure, JevDecisionProvider
from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings

PRIVATE = "private-slide-text-and-database-password"
PASSAGE = {
    "segments": ["Show the results"],
    "current_view": {},
    "options": [{"id": "c0", "tool": "no_action", "content": "Keep current view"}],
}


def test_session_integrity_error_does_not_log_sql_parameters_or_traceback(caplog, capsys):
    application = create_app(Settings(Environment.TEST, ("testserver",)))
    application.dependency_overrides[authenticated] = lambda: Principal(
        uuid4(), "owner@example.org"
    )
    # Exercise the actual HTTP handler's database exception path; no database service
    # is needed to demonstrate SQLAlchemy's private statement/parameter formatting.
    failure = IntegrityError("INSERT " + PRIVATE, {"state": PRIVATE}, ValueError(PRIVATE))
    application.state.database = SimpleNamespace(sessions=Mock(side_effect=failure))
    response = TestClient(application).post("/api/v1/sessions", json={"deck_id": str(uuid4())})
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["message"] == "Operation conflicts with stored data."
    records = [r for r in caplog.records if r.name == "buddy_api.presentation_sessions"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert PRIVATE not in response.text + caplog.text + capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "mode", "code", "consumed"),
    [
        (200, "oversized", "invalid_output", 3),
        (200, "timeout", "deadline", 0),
        (429, "oversized", "rate_limited", 0),
        (503, "oversized", "provider_unavailable", 0),
        (302, "oversized", "provider_unavailable", 0),
    ],
)
def test_jev_stream_failure_stops_reading_and_closes_response(
    status, mode, code, consumed, caplog, capsys
):
    class Stream(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            if mode == "timeout":
                raise httpx.ReadTimeout(PRIVATE)
            for _ in range(3):
                self.chunks += 1
                yield b"x" * 32768
            raise AssertionError("Provider body must not be consumed beyond the size limit")

        async def aclose(self):
            self.closed = True

    stream = Stream()

    async def run():
        def respond(request):
            assert request.url == "https://api.typesafe.ai/v1/systemone"
            return httpx.Response(
                status, stream=stream, headers={"Location": "https://other.invalid"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = JevDecisionProvider("synthetic-key", client)
            with pytest.raises(AgentFailure) as error:
                await provider.select(PASSAGE)
            assert error.value.code == code
            assert PRIVATE not in str(error.value)

    asyncio.run(run())
    assert (stream.chunks, stream.closed) == (consumed, True)
    assert PRIVATE not in caplog.text + capsys.readouterr().out


def test_jev_accepts_valid_response_at_exact_body_limit():
    body = json.dumps(
        {
            "answers": {
                "action": {"choice": "c0", "probabilities": {"c0": 0.9}},
                "presenting_now": {"noul": 0.9},
            }
        }
    ).encode()
    body += b" " * (65536 - len(body))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        ) as client:
            assert (
                await JevDecisionProvider("synthetic-key", client).select(PASSAGE)
            ).choice == "c0"

    asyncio.run(run())


@pytest.mark.parametrize("body", [b"not-json", b"[" * 2000 + b"]" * 2000])
def test_jev_malformed_or_deeply_nested_json_is_a_safe_provider_failure(body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        ) as client:
            with pytest.raises(AgentFailure, match="invalid_output"):
                await JevDecisionProvider("synthetic-key", client).select(PASSAGE)

    asyncio.run(run())
