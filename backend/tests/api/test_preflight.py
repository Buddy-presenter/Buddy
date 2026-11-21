"""Real database/API publication boundaries; synthetic artifacts test storage, not vision."""

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from backend.tests.api.test_uploads import upload_app as upload_app
from backend.tests.support.analysis import prepare

from buddy_api.analysis.contracts import digest
from buddy_api.persistence.models import ActionCheck, Deck, ProcessingJob
from buddy_api.preflight.config import PreflightSettings
from buddy_api.preflight.runner import PreflightBundle
from buddy_api.preflight.service import PreflightService
from buddy_api.presentation_candidates import CandidateAnalyses

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def explicit_worker(monkeypatch):
    async def idle(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(PreflightService, "run", idle)


def seed(client, uploads, source_format="pdf"):
    owner = UUID(client.get("/api/v1/auth/me").json()["data"]["id"])

    async def create():
        async with uploads.database.transaction() as repos:
            deck = await repos.decks.create(
                owner_id=owner, title="Preflight boundary", source_format=source_format
            )
            if source_format == "pdf":
                document = await prepare(repos, owner_id=owner, deck_id=deck.id)
            else:
                from backend.tests.support.powerpoint import manifest, output, source

                from buddy_api.analysis.powerpoint import analysis_document

                _, extraction = source()
                job = await repos.jobs.create(owner_id=owner, deck_id=deck.id)
                saved = await repos.extractions.save(
                    owner_id=owner, deck_id=deck.id, extraction=extraction
                )
                document = analysis_document(
                    output(extraction),
                    extraction,
                    manifest(extraction, str(deck.id)),
                    deck.id,
                    job_id=job.id,
                    extraction_id=saved.id,
                )
            deck.source_sha256, deck.source_revision = (
                document.source_sha256,
                document.source_revision,
            )
            deck.source_key, deck.source_version_id, deck.source_size_bytes = (
                "synthetic-source",
                "version",
                1,
            )
            record = await repos.analyses.create(
                owner_id=owner, deck_id=deck.id, version=1, document=document
            )
            deck.source_slide_count = len(document.manifest.slides)
            job = await repos.analyses.session.get(ProcessingJob, document.job_id)
            job.source_revision = document.source_revision
            # Match what processing records for this publication.
            job.status = (
                "analysis_ready" if record.publication == "published" else "analysis_partial"
            )
            job.stage_state, job.progress = "succeeded", 100
            return owner, deck.id, record.id, document, record.content_sha256

    return client.portal.call(create)


def request(client, analysis, action, *, key=None, **overrides):
    return client.post(
        f"/api/v1/analyses/{analysis}/preflights",
        headers={"Idempotency-Key": str(key or uuid4())},
        json={"instance_id": action.instance_id, **overrides},
    )


def bundle(document, action, *, corrected=False, state="validated"):
    value = action.model_dump(mode="json")
    if corrected:
        value["params"]["region"]["x"] += 1
    report = dict(
        state=state,
        reason=None if state == "validated" else "wrong_target",
        semantic_verified=state == "validated",
        action_id=action.instance_id,
        original_action_sha256=digest(action),
        corrected_action=value if state == "validated" else None,
        display_profile="wide-1x",
        history=[{"version": 0, "observation": "Synthetic storage boundary"}],
        preflight=dict(
            source_unchanged=True,
            runtime_platform="win32",
            source_sha256=document.source_sha256,
            renderer_version=document.renderer_version,
            office_version="16.synthetic" if document.source_format == "pptx" else None,
        ),
    )
    return PreflightBundle(
        report,
        {
            "report": json.dumps(report).encode(),
            "attempt-0-before_image": b"\x89PNG\r\n\x1a\nsynthetic-before",
            "attempt-0-after_image": b"\x89PNG\r\n\x1a\nsynthetic-after",
        },
    )


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_owner_private_publication_correction_and_runtime_gate(upload_app, source_format):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, original_hash = seed(client, uploads, source_format)
        action = document.slides[0].actions[0]
        key = uuid4()
        response = request(client, analysis, action, key=key)
        assert response.status_code == 202, response.text
        report_id = response.json()["data"]["id"]
        # A second old-geometry request must not override newer corrected proof.
        queued_earlier = request(client, analysis, action).json()["data"]["id"]
        assert request(client, analysis, action, key=key).json()["data"]["id"] == report_id
        assert request(client, analysis, action, key=key, correct=False).status_code == 409
        assert request(client, analysis, action, state="validated").status_code == 422
        service = PreflightService(uploads, None)
        proof = bundle(document, action, corrected=True)
        client.portal.call(service.publish, UUID(report_id), proof)
        saved = client.get(f"/api/v1/preflights/{report_id}")
        assert saved.status_code == 200 and saved.headers["Cache-Control"] == "no-store"
        data = saved.json()["data"]
        assert data["state"] == "validated" and data["corrected"]
        assert len(data["evidence"]) == 3
        assert "version" not in json.dumps(data) and "sources/" not in saved.text
        for artifact in data["evidence"]:
            response = client.get(f"/api/v1/preflights/{report_id}/evidence/{artifact['id']}")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]

        async def read():
            async with uploads.database.transaction() as repos:
                result = await repos.analyses.read(analysis, owner_id=owner)
                assert digest(result.inputs) == original_hash
                effective = result.slides[0].actions[0]
                assert effective.usable and effective.validation.semantic_verified
                assert effective.action.params.region.x == action.params.region.x + 1
                record = await repos.analyses.get(analysis, owner_id=owner)
                reader = CandidateAnalyses()
                missing = await reader.read(repos.analyses.session, record, owner)
                # Without a runtime only display-independent (PDF) evidence counts.
                assert missing.slides[0].actions[0].usable is (source_format == "pdf")
                context = dict(
                    platform="win32",
                    renderer_version=document.renderer_version,
                    display_profile="wide-1x",
                )
                matching = await reader.read(repos.analyses.session, record, owner, context)
                assert matching.slides[0].actions[0].usable
                mismatched = await reader.read(
                    repos.analyses.session, record, owner, {**context, "display_profile": "wide-2x"}
                )
                # PDF evidence holds for any display (one pinned renderer, source
                # coordinates); PowerPoint evidence is bound to the presenter's scale.
                assert mismatched.slides[0].actions[0].usable is (source_format == "pdf")

        client.portal.call(read)
        client.portal.call(service.publish, UUID(queued_earlier), bundle(document, action))
        assert client.get(f"/api/v1/preflights/{queued_earlier}").json()["data"]["phase"] == "stale"
        # A late failure must not erase completed evidence or undo accepted proof.
        client.portal.call(service.fail, UUID(report_id), "late_worker_error")
        assert client.get(f"/api/v1/preflights/{report_id}").json()["data"]["state"] == "validated"
        client.headers["Authorization"] = "Bearer account-b"
        assert client.get(f"/api/v1/preflights/{report_id}").status_code == 404
        assert (
            client.get(
                f"/api/v1/preflights/{report_id}/evidence/{data['evidence'][0]['id']}"
            ).status_code
            == 404
        )
        assert request(client, analysis, action).status_code == 404


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_automatic_scheduling_and_manual_session_fallback(upload_app, source_format):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads, source_format)
        # Pending/unavailable focus actions must not prevent a normal full-slide session.
        response = client.post("/api/v1/sessions", json={"deck_id": str(deck)})
        assert response.status_code == 200, response.text
        session = response.json()["data"]
        assert session["state"]["focus_view"] == "slide"
        action = document.slides[0].actions[0]
        rejected = client.patch(
            f"/api/v1/sessions/{session['id']}/state",
            json={
                **session["state"],
                "last_accepted_action_id": action.instance_id,
                "expected_state_version": session["state_version"],
            },
        )
        assert rejected.status_code == 422
        service = PreflightService(
            uploads, PreflightSettings(python=sys.executable), platform="win32"
        )
        client.portal.call(service.schedule)

        async def scheduled():
            from sqlalchemy import select

            async with uploads.transaction() as db:
                return list(
                    await db.scalars(select(ActionCheck).where(ActionCheck.deck_id == deck))
                )

        checks = client.portal.call(scheduled)
        total = sum(len(s.actions) for s in document.slides)
        if source_format == "pdf":
            assert len(checks) == total
            assert {c.request_data["capture"] for c in checks} == {"server"}
            client.portal.call(service.schedule)
            assert len(client.portal.call(scheduled)) == len(checks)
            identifier = next(
                c.id for c in checks if c.request_data["instance_id"] == action.instance_id
            )
        else:
            # PowerPoint is never scheduled on the server: the owner's desktop captures.
            assert checks == []
            created = request(client, analysis, action).json()["data"]
            identifier = UUID(created["id"])
            assert client.portal.call(scheduled)[0].request_data["capture"] == "desktop"
        client.portal.call(service.publish, identifier, bundle(document, action))
        # Library reports check progress; this test API has no judge or server checker.
        listed = client.get("/api/v1/decks").json()["data"]["items"]
        progress = next(d for d in listed if d["id"] == str(deck))["processing"]
        assert progress["state"] in {"review_ready", "partial"}
        assert progress["checks"] == {
            "total": total,
            "checked": 1,
            "ready": 1,
            "available": False,
            "active": False,
            "mode": "desktop" if source_format == "pptx" else "server",
        }
        context = dict(
            platform="win32", renderer_version=document.renderer_version, display_profile="wide-1x"
        )
        resumed = client.post(
            "/api/v1/sessions", json={"deck_id": str(deck), "runtime": context}
        ).json()["data"]
        accepted = client.patch(
            f"/api/v1/sessions/{resumed['id']}/state",
            json={
                **resumed["state"],
                "last_accepted_action_id": action.instance_id,
                "expected_state_version": resumed["state_version"],
            },
        )
        assert accepted.status_code == 200, accepted.text
        switched = client.post(
            "/api/v1/sessions",
            json={"deck_id": str(deck), "runtime": {**context, "display_profile": "wide-2x"}},
        ).json()["data"]
        assert switched["state"]["last_accepted_action_id"] is None
        assert switched["state_version"] > accepted.json()["data"]["state_version"]


BEFORE = b"\x89PNG\r\n\x1a\ndesktop-before"
AFTER = b"\x89PNG\r\n\x1a\ndesktop-after"


def desktop_report(document, action, state):
    report = bundle(document, action, state=state).report
    report["history"] = [{"version": 0, "state": state, "observation": "Desktop capture"}]
    return report


def png(data):
    import base64

    return base64.b64encode(data).decode()


@pytest.mark.parametrize("verdict", ["validated", "rejected", "uncertain"])
def test_desktop_capture_is_judged_and_bound_by_the_server(upload_app, verdict):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads, "pptx")
        action = document.slides[0].actions[0]
        calls = []

        def judge_factory():
            def judge(target, before, after):
                calls.append((target, before, after))
                return dict(
                    verdict="rejected" if verdict == "rejected" else "validated",
                    confidence=0.5 if verdict == "uncertain" else 0.95,
                    issue="wrong_target" if verdict == "rejected" else None,
                    observation="Synthetic verdict",
                    correction=None,
                )

            return judge

        client.app.state.preflight = PreflightService(uploads, None, judge_factory=judge_factory)
        created = request(client, analysis, action).json()["data"]
        identifier = created["id"]
        # The desktop cannot judge before claiming, and claims a bound snapshot.
        early = client.post(
            f"/api/v1/preflights/{identifier}/judge",
            json={"sequence": 1, "target": {}, "before": png(BEFORE), "after": png(AFTER)},
        )
        assert early.status_code == 409
        snapshot = client.post(f"/api/v1/preflights/{identifier}/claim").json()["data"]
        assert snapshot["source_format"] == "pptx"
        assert snapshot["action"]["instance_id"] == action.instance_id
        assert snapshot["source_sha256"] == document.source_sha256
        judged = client.post(
            f"/api/v1/preflights/{identifier}/judge",
            json={
                "sequence": 1,
                "target": {"purpose": "x"},
                "before": png(BEFORE),
                "after": png(AFTER),
            },
        )
        assert judged.status_code == 200, judged.text
        # An uncertain approval is recorded as a rejection, like the PDF path.
        decided = "validated" if verdict == "validated" else "rejected"
        assert judged.json()["data"]["verdict"] == decided
        assert calls and calls[0][1] == BEFORE
        # Out-of-order attempts are refused.
        assert (
            client.post(
                f"/api/v1/preflights/{identifier}/judge",
                json={"sequence": 3, "target": {}, "before": png(BEFORE), "after": png(AFTER)},
            ).status_code
            == 409
        )
        # The desktop cannot claim a different outcome or swap screenshots.
        flipped = "rejected" if decided == "validated" else "validated"
        for report, images in (
            (
                desktop_report(document, action, flipped),
                {"attempt-0-before_image": png(BEFORE), "attempt-0-after_image": png(AFTER)},
            ),
            (
                desktop_report(document, action, decided),
                {"attempt-0-before_image": png(AFTER), "attempt-0-after_image": png(AFTER)},
            ),
        ):
            response = client.post(
                f"/api/v1/preflights/{identifier}/result",
                json={"report": report, "images": images},
            )
            assert response.status_code == 409, response.text
        published = client.post(
            f"/api/v1/preflights/{identifier}/result",
            json={
                "report": desktop_report(document, action, decided),
                "images": {
                    "attempt-0-before_image": png(BEFORE),
                    "attempt-0-after_image": png(AFTER),
                },
            },
        )
        assert published.status_code == 200, published.text
        assert published.json()["data"]["state"] == decided

        async def usable():
            async with uploads.database.transaction() as repos:
                result = await repos.analyses.read(analysis, owner_id=owner)
                return result.slides[0].actions[0].usable

        assert client.portal.call(usable) is (decided == "validated")
        # Another account can neither claim nor read it.
        client.headers["Authorization"] = "Bearer account-b"
        assert client.post(f"/api/v1/preflights/{identifier}/claim").status_code == 404


def test_desktop_failure_is_recorded_without_enabling_the_action(upload_app):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads, "pptx")
        action = document.slides[0].actions[0]
        client.app.state.preflight = PreflightService(
            uploads, None, judge_factory=lambda: lambda *_: None
        )
        identifier = request(client, analysis, action).json()["data"]["id"]
        client.post(f"/api/v1/preflights/{identifier}/claim")
        failed = client.post(
            f"/api/v1/preflights/{identifier}/fail",
            json={"reason": "audience_viewport_occluded"},
        )
        assert failed.status_code == 200, failed.text
        data = failed.json()["data"]
        assert data["state"] == "unavailable"
        assert data["reasons"] == ["audience_viewport_occluded"]
        # Unknown reasons are normalized rather than stored verbatim.
        second = request(client, analysis, action).json()["data"]["id"]
        client.post(f"/api/v1/preflights/{second}/claim")
        other = client.post(f"/api/v1/preflights/{second}/fail", json={"reason": "made_up"})
        assert other.json()["data"]["reasons"] == ["desktop_check_failed"]


def test_server_pdf_check_hands_off_to_visual_qa_without_holding_its_slot(upload_app):
    """The PDF server checker takes the execution slot itself (regression)."""
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads)
        action = document.slides[0].actions[0]
        identifier = UUID(request(client, analysis, action).json()["data"]["id"])
        calls = []

        class VisualQA:
            async def verify(self, owner_id, analysis_id, instance_id, request_id):
                calls.append((instance_id, request_id))
                assert not uploads.parser_slot.locked()
                async with uploads.parser_slot, uploads.database.transaction() as repos:
                    return await repos.analyses.read(analysis_id, owner_id=owner_id)

        service = PreflightService(uploads, None, visual_qa=VisualQA())
        assert client.portal.call(service.process_one)
        assert calls == [(action.instance_id, identifier)]
        saved = client.get(f"/api/v1/preflights/{identifier}").json()["data"]
        assert saved["phase"] == "complete" and saved["reasons"] != ["preflight_failed"]


@pytest.mark.parametrize("reason", ["unsupported", "interrupted", "stale", "quota"])
def test_terminal_failures_and_stale_source_do_not_enable_actions(upload_app, reason):
    with upload_app(quota_bytes=1000 if reason == "quota" else 1024**3) as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads)
        action = document.slides[0].actions[0]
        response = request(client, analysis, action)
        identifier = UUID(response.json()["data"]["id"])
        service = PreflightService(uploads, None)

        async def expire_or_change():
            async with uploads.transaction() as db:
                row = await db.get(ActionCheck, identifier)
                if reason == "interrupted":
                    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    source = await db.get(Deck, deck)
                    source.source_sha256 = "f" * 64

        if reason in {"interrupted", "stale"}:
            client.portal.call(expire_or_change)
        if reason == "interrupted":
            client.portal.call(service.reconcile)
        elif reason == "stale":
            client.portal.call(service.publish, identifier, bundle(document, action))
        else:
            assert client.portal.call(service.process_one)
        saved = client.get(f"/api/v1/preflights/{identifier}").json()["data"]
        assert saved["state"] == "unavailable"
        assert saved["phase"] != "running" and saved["reasons"]

        async def checked():
            async with uploads.database.transaction() as repos:
                result = await repos.analyses.read(analysis, owner_id=owner)
                assert not result.slides[0].actions[0].usable
                if reason != "stale":
                    assert result.slides[0].actions[0].validation.reasons

        client.portal.call(checked)


def test_lost_storage_acknowledgement_is_cleaned_before_terminal_publication(upload_app):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads)
        action = document.slides[0].actions[0]
        identifier = UUID(request(client, analysis, action).json()["data"]["id"])
        service = PreflightService(uploads, None)
        cloud.fail_after_put = True
        from buddy_api.uploads.contracts import UploadError

        with pytest.raises(UploadError):
            client.portal.call(service.publish, identifier, bundle(document, action))
        assert cloud.objects
        client.portal.call(service.fail, identifier, "storage_unavailable")
        assert not cloud.objects
        saved = client.get(f"/api/v1/preflights/{identifier}").json()["data"]
        assert saved["state"] == "unavailable" and not saved["evidence"]

        async def no_charge():
            async with uploads.transaction() as db:
                row = await db.get(ActionCheck, identifier)
                assert row.storage_bytes == 0 and row.status == "complete"

        client.portal.call(no_charge)


def test_new_graph_schedules_new_proof_instead_of_reusing_old_result(upload_app):
    with upload_app() as (client, cloud, uploads):
        owner, deck, analysis, document, _ = seed(client, uploads)
        service = PreflightService(
            uploads, PreflightSettings(python=sys.executable), platform="win32"
        )
        client.portal.call(service.schedule)

        async def revise():
            async with uploads.database.transaction() as repos:
                saved = await repos.analyses.create(
                    owner_id=owner, deck_id=deck, version=2, document=document
                )
                return saved.id

        updated = client.portal.call(revise)
        client.portal.call(service.schedule)

        async def verify():
            from sqlalchemy import select

            async with uploads.transaction() as db:
                rows = list(
                    await db.scalars(select(ActionCheck).where(ActionCheck.deck_id == deck))
                )
                assert {r.request_data["analysis_id"] for r in rows} == {
                    str(analysis),
                    str(updated),
                }

        client.portal.call(verify)
