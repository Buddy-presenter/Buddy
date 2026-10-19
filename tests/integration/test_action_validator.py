"""Real isolated executor, deterministic predefined actions, and private API evidence."""

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from backend.tests.api.conftest import migrated_database as migrated_database
from backend.tests.api.test_uploads import (
    initialize,
    metadata,
)
from backend.tests.api.test_uploads import (
    upload_app as upload_app,
)
from backend.tests.support.database import database_settings as database_settings

from buddy_api.persistence.models import ActionCheck, Deck
from buddy_api.uploads.contracts import UploadError
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ProcessRequest, ProcessResult
from buddy_api.validation.runner import run_validation
from buddy_api.validation.service import ValidationService

PDF = Path(__file__).resolve().parents[1] / "fixtures/slides/Buddy_PDF_Geometry.pdf"
CONTENT = PDF.read_bytes()
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def action(deck, revision="v1", index=0, kind="zoom_region"):
    sid = str(uuid5(NAMESPACE_URL, f"buddy:{DIGEST}:{revision}:pdf:{index}"))
    params = (
        {"region": {"x": 96, "y": 410, "width": 480, "height": 240}}
        if kind in {"zoom_region", "highlight_region"}
        else {}
    )
    if kind == "go_to_slide":
        params = {"target_slide_id": str(uuid5(NAMESPACE_URL, f"buddy:{DIGEST}:{revision}:pdf:2"))}
    return {
        "version": "1.1",
        "instance_id": "predefined-action",
        "purpose": "Test the selected region.",
        "deck_id": str(deck),
        "render_id": "pdfium-5.14.0-144dpi:" + DIGEST,
        "slide_id": sid,
        "type": kind,
        "params": params,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "zoom_region",
        "highlight_region",
        "next_slide",
        "previous_slide",
        "go_to_slide",
        "reset_view",
    ],
)
def test_all_six_actions_execute_in_real_isolated_qt_with_surface_evidence(kind):
    deck = uuid4()
    request = ProcessRequest(
        deck_id=deck, source_revision="v1", source_sha256=DIGEST, action=action(deck, kind=kind)
    )
    result = run_validation(CONTENT, request, ValidatorSettings(python=sys.executable))
    assert result.result.state == "validated", result.result.model_dump_json()
    assert not result.result.semantic_verified
    assert result.result.before and result.result.after
    assert set(result.artifacts) == {"before_image", "after_image", "report"}
    assert result.artifacts["before_image"].startswith(b"\x89PNG")
    if kind == "reset_view":
        assert result.result.before.highlight is not None and result.result.after.highlight is None
        assert result.result.before.region != result.result.after.region


@pytest.mark.parametrize(
    "kind,index,version",
    [("previous_slide", 1, "1.1"), ("next_slide", 2, "1.1"), ("next_slide", 0, "1.0")],
)
def test_navigation_from_other_pages_boundaries_and_original_catalog(kind, index, version):
    deck = uuid4()
    command = {**action(deck, index=index, kind=kind), "version": version}
    result = run_validation(
        CONTENT,
        ProcessRequest(deck_id=deck, source_revision="v1", source_sha256=DIGEST, action=command),
        ValidatorSettings(python=sys.executable),
    )
    assert result.result.state == "validated", result.result.model_dump_json()


@pytest.mark.parametrize("profile", ["standard-1x", "portrait-1x", "wide-2x"])
def test_display_profiles_and_rotated_pdf_page(profile):
    deck = uuid4()
    command = action(deck, index=1, kind="highlight_region")
    command["params"] = {"region": {"x": 50, "y": 50, "width": 100, "height": 40}}
    result = run_validation(
        CONTENT,
        ProcessRequest(
            deck_id=deck,
            source_revision="v1",
            source_sha256=DIGEST,
            action=command,
            profile=profile,
        ),
        ValidatorSettings(python=sys.executable),
    )
    assert result.result.state == "validated", result.result.model_dump_json()


@pytest.mark.parametrize(
    "change",
    [
        {"type": "open_url", "params": {"url": "https://example.com"}},
        {"render_id": "stale"},
        {"slide_id": "missing"},
        {"params": {"region": {"x": -1, "y": 0, "width": 100, "height": 100}}},
        {"params": {"region": {"x": 1400, "y": 0, "width": 100, "height": 100}}},
        {"params": {"region": {"x": 0, "y": 0, "width": 1, "height": 1}}},
    ],
)
def test_invalid_actions_are_rejected_without_semantic_approval(change):
    deck = uuid4()
    result = run_validation(
        CONTENT,
        ProcessRequest(
            deck_id=deck,
            source_revision="v1",
            source_sha256=DIGEST,
            action={**action(deck), **change},
        ),
        ValidatorSettings(python=sys.executable),
    )
    assert result.result.state == "rejected", result.result.model_dump_json()
    assert result.result.reasons and not result.result.semantic_verified


def test_cancellation_is_not_a_success_and_transport_schemas_match():
    deck, cancelled = uuid4(), Event()
    cancelled.set()
    result = run_validation(
        CONTENT,
        ProcessRequest(
            deck_id=deck, source_revision="v1", source_sha256=DIGEST, action=action(deck)
        ),
        ValidatorSettings(python=sys.executable),
        cancelled,
    )
    assert result.result.state == "needs-review" and result.result.reasons == ["cancelled"]
    for name, model in (("request", ProcessRequest), ("result", ProcessResult)):
        schema = json.loads(
            files("buddy_desktop")
            .joinpath("validation_contracts", name + "-v1.schema.json")
            .read_text()
        )
        assert schema == model.model_json_schema()


@pytest.mark.parametrize("hang", [False, True])
def test_real_child_crash_or_timeout_is_never_validated(monkeypatch, hang):
    from buddy_api.validation import runner

    original = runner.subprocess.Popen
    code = "import sys,time;sys.stdin.buffer.read(1);" + (
        "time.sleep(20)" if hang else "sys.exit(7)"
    )

    def launch(_command, **kwargs):
        return original([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    deck = uuid4()
    result = run_validation(
        CONTENT,
        ProcessRequest(
            deck_id=deck, source_revision="v1", source_sha256=DIGEST, action=action(deck)
        ),
        ValidatorSettings(python=sys.executable, seconds=5),
    )
    assert result.result.state == "needs-review"
    assert result.result.reasons == ["time_limit" if hang else "worker_failed"]


@pytest.mark.postgres
def test_owner_api_real_validation_private_evidence_and_idempotency(upload_app):
    with upload_app() as (client, cloud, uploads):
        client.app.state.validator = ValidationService(
            uploads, ValidatorSettings(python=sys.executable)
        )
        metadata_body = {
            **metadata(CONTENT),
            "filename": "slides.pdf",
            "content_type": "application/pdf",
        }
        identifier = initialize(client, body=metadata_body).json()["data"]["id"]
        saved = client.put(
            f"/api/v1/uploads/{identifier}/content",
            content=CONTENT,
            headers={"Content-Type": "application/pdf"},
        )
        assert saved.status_code == 200, saved.text
        deck = saved.json()["data"]["deck"]
        body = {"action": action(identifier, revision=deck["source_revision"])}
        key = str(uuid4())
        endpoint = f"/api/v1/decks/{identifier}/action-validations"
        response = client.post(endpoint, json=body, headers={"Idempotency-Key": key})
        assert response.status_code == 200, response.text
        value = response.json()["data"]
        assert value["result"]["state"] == "validated", value
        assert len(value["evidence"]) == 3
        assert "sources/" not in response.text
        assert all(set(ref) == {"id", "kind", "sha256", "size_bytes"} for ref in value["evidence"])
        puts = cloud.puts
        again = client.post(endpoint, json=body, headers={"Idempotency-Key": key})
        assert again.json()["data"] == value and cloud.puts == puts
        ref = value["evidence"][0]
        link = f"/api/v1/action-validations/{value['id']}/evidence/{ref['id']}"
        evidence = client.get(link)
        assert evidence.status_code == 200 and evidence.headers["cache-control"] == "no-store"
        assert hashlib.sha256(evidence.content).hexdigest() == ref["sha256"]
        client.headers["Authorization"] = "Bearer account-b"
        assert client.get(link).status_code == 404
        assert client.get(f"/api/v1/action-validations/{value['id']}").status_code == 404
        assert (
            client.post(endpoint, json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
            == 404
        )
        client.headers["Authorization"] = "Bearer account-a"
        assert (
            client.post(
                endpoint,
                json={**body, "state": "validated"},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 422
        )
        body["action"]["render_id"] = "old-render"
        rejected = client.post(endpoint, json=body, headers={"Idempotency-Key": str(uuid4())})
        assert rejected.json()["data"]["result"]["state"] == "rejected"


@pytest.mark.postgres
def test_evidence_failure_cleanup_retries_and_expired_run_reconciliation(upload_app):
    with upload_app() as (client, cloud, uploads):
        validator = ValidationService(uploads, ValidatorSettings(python=sys.executable))
        client.app.state.validator = validator
        body = {**metadata(CONTENT), "filename": "deck.pdf", "content_type": "application/pdf"}
        identifier = initialize(client, body=body).json()["data"]["id"]
        deck = client.put(
            f"/api/v1/uploads/{identifier}/content",
            content=CONTENT,
            headers={"Content-Type": "application/pdf"},
        ).json()["data"]["deck"]
        original_key = next(iter(cloud.objects))
        request = {"action": action(identifier, revision=deck["source_revision"])}
        endpoint = f"/api/v1/decks/{identifier}/action-validations"
        key = str(uuid4())
        cloud.fail_after_put = True
        assert (
            client.post(endpoint, json=request, headers={"Idempotency-Key": key}).status_code == 503
        )
        assert list(cloud.objects) == [original_key]
        cloud.fail_after_put = False
        saved = client.post(endpoint, json=request, headers={"Idempotency-Key": key})
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["result"]["state"] == "validated"

        async def interrupted():
            async with uploads.transaction() as session:
                from sqlalchemy import select

                accepted = await session.scalar(
                    select(ActionCheck).where(ActionCheck.status == "complete")
                )
                orphan = ActionCheck(
                    id=uuid4(),
                    owner_id=accepted.owner_id,
                    deck_id=accepted.deck_id,
                    retry_key=uuid4(),
                    request_sha256="a" * 64,
                    storage_bytes=32 * 1024 * 1024,
                    status="pending",
                    artifacts=[],
                    expires_at=datetime.now(UTC) - timedelta(minutes=1),
                )
                session.add(orphan)
            orphan_key = validator.key(orphan, "after_image")
            cloud.objects[orphan_key] = ("orphan-version", b"incomplete")
            cloud.fail_delete = True
            with pytest.raises(UploadError):
                await validator.reconcile()
            cloud.fail_delete = False
            await validator.reconcile()
            assert orphan_key not in cloud.objects and original_key in cloud.objects
            async with uploads.transaction() as session:
                cleaned = await session.get(ActionCheck, orphan.id)
                assert cleaned.status == "failed" and cleaned.storage_bytes == 0
                assert (await session.get(ActionCheck, accepted.id)).status == "complete"

        client.portal.call(interrupted)


@pytest.mark.postgres
def test_interrupted_validation_reservation_is_reclaimed_and_retried(upload_app):
    with upload_app() as (client, cloud, uploads):
        validator = ValidationService(uploads, ValidatorSettings(python=sys.executable))
        client.app.state.validator = validator
        body = {**metadata(CONTENT), "filename": "deck.pdf", "content_type": "application/pdf"}
        identifier = initialize(client, body=body).json()["data"]["id"]
        deck = client.put(
            f"/api/v1/uploads/{identifier}/content",
            content=CONTENT,
            headers={"Content-Type": "application/pdf"},
        ).json()["data"]["deck"]
        request = {"action": action(identifier, revision=deck["source_revision"])}
        retry_key = uuid4()
        process_request = ProcessRequest(
            deck_id=UUID(identifier),
            source_revision=deck["source_revision"],
            source_sha256=DIGEST,
            **request,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                process_request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        reservation_id = uuid4()

        async def reserve():
            async with uploads.transaction() as session:
                deck_row = await session.get(Deck, UUID(identifier))
                assert deck_row is not None
                session.add(
                    ActionCheck(
                        id=reservation_id,
                        owner_id=deck_row.owner_id,
                        deck_id=deck_row.id,
                        retry_key=retry_key,
                        request_sha256=fingerprint,
                        storage_bytes=32 * 1024 * 1024,
                        status="pending",
                        artifacts=[],
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    )
                )

        client.portal.call(reserve)
        endpoint = f"/api/v1/decks/{identifier}/action-validations"
        waiting = client.post(endpoint, json=request, headers={"Idempotency-Key": str(retry_key)})
        assert waiting.status_code == 409
        assert waiting.headers["retry-after"] == "300"
        assert waiting.json()["error"]["code"] == "validation_pending"

        async def expire_and_cleanup():
            async with uploads.transaction() as session:
                row = await session.get(ActionCheck, reservation_id)
                assert row is not None
                row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await validator.reconcile()

        client.portal.call(expire_and_cleanup)
        recovered = client.post(endpoint, json=request, headers={"Idempotency-Key": str(retry_key)})
        assert recovered.status_code == 200, recovered.text
        result = recovered.json()["data"]
        assert result["id"] == str(reservation_id)
        assert result["result"]["state"] == "validated"
        assert len(result["evidence"]) == 3

        async def completed_status():
            async with uploads.transaction() as session:
                row = await session.get(ActionCheck, reservation_id)
                assert row is not None
                return row.status

        assert client.portal.call(completed_status) == "complete"
