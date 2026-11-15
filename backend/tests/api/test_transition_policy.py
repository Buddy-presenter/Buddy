"""Fresh, owner-bound pre-execution eligibility against real PostgreSQL state."""

from uuid import uuid4

import pytest
from backend.tests.api.test_authorization import private_api as private_api
from backend.tests.api.test_authorization import ready_session_api as ready_session_api
from backend.tests.support.database import database_settings as database_settings

from buddy_api.persistence.models import PresentationSession


def test_transition_input_is_bounded_and_errors_omit_private_values(api_client):
    path = f"/api/v1/sessions/{uuid4()}/transition-check"
    response = api_client.post(path, content=b"x" * 16385)
    assert response.status_code == 413
    response = api_client.post(path, json={"private_field": "do-not-echo-this-value"})
    assert response.status_code == 422
    assert "do-not-echo-this-value" not in response.text


@pytest.mark.postgres
def test_transition_requires_opt_in_owner_current_state_and_authoritative_target(ready_session_api):
    client, ids, *_ = ready_session_api
    path = f"/api/v1/sessions/{ids['sessions']}"
    headers = {"Authorization": "Bearer owner"}
    original = client.get(path, headers=headers).json()["data"]
    state = original["state"]
    target = {"tool": "next_slide", "slide_id": "slide-b", "action_id": None, "region": None}
    payload = {
        "expected_state_version": 4,
        "analysis_id": original["analysis_version_id"],
        "target": target,
    }
    assert client.post(path + "/transition-check", headers=headers, json=payload).status_code == 409
    response = client.patch(
        path + "/state",
        headers=headers,
        json={**state, "expected_state_version": 4, "autonomy_mode": "autonomous"},
    )
    assert response.status_code == 200, response.text
    payload["expected_state_version"] = 5
    for token, status in ((None, 401), ("other", 403), ("owner", 200)):
        response = client.post(
            path + "/transition-check",
            json=payload,
            headers={"Authorization": "Bearer " + token} if token else {},
        )
        assert response.status_code == status, response.text
    assert response.json()["target"] == target
    assert response.headers["cache-control"] == "no-store"
    before = client.get(path, headers=headers).json()["data"]
    for changes in (
        {"expected_state_version": 4},
        {"analysis_id": str(uuid4())},
        {"target": {**target, "slide_id": "not-in-deck"}},
        {"target": {**target, "tool": "previous_slide"}},
        {"target": {**target, "action_id": "invented"}},
        {
            "target": {
                "tool": "zoom_region",
                "slide_id": "slide-a",
                "action_id": "missing",
                "region": {"x": 1, "y": 1, "width": 20, "height": 20},
            }
        },
    ):
        assert (
            client.post(
                path + "/transition-check", headers=headers, json={**payload, **changes}
            ).status_code
            == 409
        )
    assert client.get(path, headers=headers).json()["data"] == before

    async def end():
        async with client.app.state.database.sessions() as db, db.begin():
            (await db.get(PresentationSession, ids["sessions"])).status = "ended"

    client.portal.call(end)
    assert client.post(path + "/transition-check", headers=headers, json=payload).status_code == 409
