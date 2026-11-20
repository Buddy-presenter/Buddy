"""Durable owner corrections over immutable PDF and PowerPoint analysis graphs."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from buddy_api.analysis.contracts import EvidenceReference, Region, ValidationInput, digest
from buddy_api.analysis.corrections import (
    ActionCorrectionRequest,
    correct_action,
)
from buddy_api.analysis.powerpoint import analysis_document
from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.persistence.models import ActionCheck, ActionValidation, AnalysisCorrection
from buddy_api.validation.service import ValidationService


@pytest.mark.postgres
@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_owner_correction_creates_immutable_revision_and_preserves_session(
    migrated_database, source_format
):
    async def scenario():
        db = Database(migrated_database)
        try:
            async with db.transaction() as repos:
                owner = await repos.users.create("correction-owner")
                other = await repos.users.create("correction-other")
                deck = await repos.decks.create(
                    owner_id=owner.id, title="Correction fixture", source_format=source_format
                )
                if source_format == "pdf":
                    from backend.tests.support.analysis import prepare

                    inputs = await prepare(repos, owner_id=owner.id, deck_id=deck.id)
                else:
                    from backend.tests.support.powerpoint import manifest, output, source

                    _, extracted = source()
                    job = await repos.jobs.create(owner_id=owner.id, deck_id=deck.id)
                    saved_extraction = await repos.extractions.save(
                        owner_id=owner.id, deck_id=deck.id, extraction=extracted
                    )
                    inputs = analysis_document(
                        output(extracted),
                        extracted,
                        manifest(extracted, str(deck.id)),
                        deck.id,
                        job.id,
                        saved_extraction.id,
                    )
                deck.source_revision = inputs.source_revision
                deck.source_sha256 = inputs.source_sha256
                deck.source_version_id = "fixture-source-version"
                deck.source_slide_count = len(inputs.slides)
                saved = await repos.analyses.create(
                    owner_id=owner.id, deck_id=deck.id, version=1, document=inputs
                )
                before = await repos.analyses.read(saved.id, owner_id=owner.id)
                preserved_action = before.slides[1].actions[0].action
                evidence_id = uuid4()
                artifact = {
                    "id": str(evidence_id),
                    "kind": "report",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                    "version": "fixture-object-version",
                }
                source_validation_id = uuid4()
                source_check = ActionCheck(
                    id=source_validation_id,
                    owner_id=owner.id,
                    deck_id=deck.id,
                    retry_key=uuid4(),
                    request_sha256="b" * 64,
                    storage_bytes=1,
                    status="complete",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    result={"source_format": source_format},
                    artifacts=[artifact],
                )
                repos.analyses.session.add(source_check)
                await repos.analyses.session.flush()
                prior_validation = ValidationInput(
                    source_format=source_format,
                    runtime_version="Office-fixture" if source_format == "pptx" else None,
                    validation_id=source_check.id,
                    analysis_id=saved.id,
                    analysis_sha256=before.analysis_sha256,
                    instance_id=preserved_action.instance_id,
                    action_sha256=digest(preserved_action),
                    validator_version=inputs.validator_version,
                    renderer_version=inputs.renderer_version,
                    display_profile=inputs.display_profile,
                    state="needs-review",
                    reasons=("Synthetic proof awaiting human review",),
                    evidence=(EvidenceReference(id=evidence_id, kind="report", sha256="a" * 64),),
                )
                await repos.analyses.record_validation(owner_id=owner.id, result=prior_validation)
                before = await repos.analyses.read(saved.id, owner_id=owner.id)
                pinned = await repos.sessions.create(
                    owner_id=owner.id, deck_id=deck.id, analysis_version_id=saved.id
                )
                original_action = before.slides[0].actions[0].action
                old_region = original_action.params.region
                changed_region = Region(
                    x=old_region.x + 1,
                    y=old_region.y,
                    width=old_region.width,
                    height=old_region.height,
                )
                request_id = uuid4()
                correction = ActionCorrectionRequest(
                    expected_analysis_sha256=before.analysis_sha256,
                    expected_action_sha256=digest(original_action),
                    operation="set_region",
                    region=changed_region,
                )
                if source_format == "pdf":
                    source_plane = inputs.manifest.slides[0].source
                    for invalid_region in (
                        Region(x=source_plane.width + 1, y=0, width=1, height=1),
                        Region(x=0, y=source_plane.height + 1, width=1, height=1),
                        Region(x=source_plane.width - 1, y=0, width=2, height=1),
                        Region(x=0, y=source_plane.height - 1, width=1, height=2),
                    ):
                        with pytest.raises(ValueError, match="extends beyond"):
                            await correct_action(
                                repos.analyses.session,
                                owner_id=owner.id,
                                analysis_id=saved.id,
                                instance_id=original_action.instance_id,
                                request_id=uuid4(),
                                inputs=correction.model_copy(update={"region": invalid_region}),
                            )
                first = await correct_action(
                    repos.analyses.session,
                    owner_id=owner.id,
                    analysis_id=saved.id,
                    instance_id=original_action.instance_id,
                    request_id=request_id,
                    inputs=correction,
                )
                retried = await correct_action(
                    repos.analyses.session,
                    owner_id=owner.id,
                    analysis_id=saved.id,
                    instance_id=original_action.instance_id,
                    request_id=request_id,
                    inputs=correction,
                )
                assert first == retried
                assert first.result_version == 2
                updated = await repos.analyses.read(first.result_analysis_id, owner_id=owner.id)
                updated_action = updated.slides[0].actions[0]
                assert updated_action.action.params.region == changed_region
                assert updated_action.state == "candidate"
                copied_action = updated.slides[1].actions[0]
                assert copied_action.state == "needs-review"
                assert copied_action.validation is not None
                assert copied_action.validation.validation_id != source_check.id
                copied_validation = await repos.analyses.session.get(
                    ActionValidation, copied_action.validation.validation_id
                )
                assert copied_validation is not None
                assert copied_validation.evidence_source_validation_id == source_check.id

                class UploadTransactions:
                    @asynccontextmanager
                    async def transaction(self):
                        # Reuse this transaction so the synthetic private proof rows,
                        # not yet committed by the outer fixture, are visible.
                        yield repos.analyses.session

                validation_service = ValidationService(UploadTransactions())  # type: ignore[arg-type]
                origin, copied_artifact = await validation_service._evidence_source(
                    owner.id, copied_action.validation.validation_id, evidence_id
                )
                assert origin == source_check.id
                assert copied_artifact == artifact
                assert await repos.analyses.read(saved.id, owner_id=owner.id) == before
                assert (
                    await repos.sessions.get(pinned.id, owner_id=owner.id)
                ).analysis_version_id == saved.id

                with pytest.raises(PersistenceConflict):
                    await correct_action(
                        repos.analyses.session,
                        owner_id=owner.id,
                        analysis_id=saved.id,
                        instance_id=original_action.instance_id,
                        request_id=uuid4(),
                        inputs=correction,
                    )
                with pytest.raises(RecordNotFound):
                    await correct_action(
                        repos.analyses.session,
                        owner_id=other.id,
                        analysis_id=saved.id,
                        instance_id=original_action.instance_id,
                        request_id=uuid4(),
                        inputs=correction,
                    )

                disable = ActionCorrectionRequest(
                    expected_analysis_sha256=updated.analysis_sha256,
                    expected_action_sha256=digest(updated_action.action),
                    operation="disable",
                )
                disabled = await correct_action(
                    repos.analyses.session,
                    owner_id=owner.id,
                    analysis_id=updated.id,
                    instance_id=original_action.instance_id,
                    request_id=uuid4(),
                    inputs=disable,
                )
                final = await repos.analyses.read(disabled.result_analysis_id, owner_id=owner.id)
                assert final.version == 3
                assert all(
                    action.action.instance_id != original_action.instance_id
                    for slide in final.slides
                    for action in slide.actions
                )
                correction_rows = list(
                    await repos.analyses.session.scalars(
                        select(AnalysisCorrection).where(
                            AnalysisCorrection.owner_id == owner.id,
                            AnalysisCorrection.deck_id == deck.id,
                        )
                    )
                )
                assert [row.operation for row in correction_rows] == ["set_region", "disable"]
                assert (
                    await repos.sessions.get(pinned.id, owner_id=owner.id)
                ).analysis_version_id == saved.id
        finally:
            await db.close()

    asyncio.run(scenario())
