"""Owner-authored geometry changes as immutable analysis revisions."""

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buddy_api.analysis.contracts import (
    AnalysisInput,
    AnalysisResult,
    Model,
    PowerPointAnalysisResult,
    Region,
    ValidationInput,
    digest,
    parse_analysis_input,
)
from buddy_api.analysis.store import Analyses
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.persistence.models import (
    ActionValidation,
    AnalysisCorrection,
    AnalysisVersion,
    Deck,
)


class ActionCorrectionRequest(Model):
    expected_analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_action_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["set_region", "disable"]
    region: Region | None = None

    @model_validator(mode="after")
    def operation_region(self) -> "ActionCorrectionRequest":
        if (self.operation == "set_region") != (self.region is not None):
            raise ValueError("A region is required only for set_region corrections.")
        return self


class ActionCorrectionOutcome(Model):
    source_analysis_id: UUID
    result_analysis_id: UUID
    source_version: int = Field(ge=1)
    result_version: int = Field(ge=2)
    action_instance_id: str = Field(min_length=1, max_length=128)
    operation: Literal["set_region", "disable"]
    action_state: Literal["candidate", "validated", "rejected", "needs-review"] | None
    usable: bool


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


async def correct_action(
    session: AsyncSession,
    *,
    owner_id: UUID,
    analysis_id: UUID,
    instance_id: str,
    request_id: UUID,
    inputs: ActionCorrectionRequest,
) -> ActionCorrectionOutcome:
    """Create a new saved analysis version, preserving all prior sessions and proofs."""
    request_document = {
        "analysis_id": str(analysis_id),
        "instance_id": instance_id,
        **inputs.model_dump(mode="json"),
    }
    request_sha = _sha256(request_document)
    prior = await session.scalar(
        select(AnalysisCorrection)
        .where(AnalysisCorrection.owner_id == owner_id, AnalysisCorrection.request_id == request_id)
        .with_for_update()
    )
    if prior is not None:
        if prior.request_sha256 != request_sha:
            raise PersistenceConflict("Correction request ID already has different content.")
        result = await Analyses(session).read(prior.result_analysis_id, owner_id=owner_id)
        action = next(
            (
                candidate
                for slide in result.slides
                for candidate in slide.actions
                if candidate.action.instance_id == instance_id
            ),
            None,
        )
        return ActionCorrectionOutcome(
            source_analysis_id=prior.source_analysis_id,
            result_analysis_id=prior.result_analysis_id,
            source_version=prior.source_version,
            result_version=prior.result_version,
            action_instance_id=instance_id,
            operation=inputs.operation,
            action_state=action.state if action else None,
            usable=action.usable if action else False,
        )

    record = await session.scalar(
        select(AnalysisVersion).where(
            AnalysisVersion.id == analysis_id, AnalysisVersion.owner_id == owner_id
        )
    )
    if record is None:
        raise RecordNotFound("Owned analysis was not found.")
    deck = await session.scalar(
        select(Deck).where(Deck.id == record.deck_id, Deck.owner_id == owner_id).with_for_update()
    )
    if deck is None or deck.deletion_requested_at is not None:
        raise RecordNotFound("Owned presentation was not found.")
    # Deck-first locking matches analysis creation and deck deletion. Refresh the
    # analysis after the lock so a competing correction cannot fork the same version.
    record = await session.scalar(
        select(AnalysisVersion)
        .where(AnalysisVersion.id == analysis_id, AnalysisVersion.owner_id == owner_id)
        .with_for_update()
    )
    if record is None:
        raise RecordNotFound("Owned analysis was not found.")
    prior = await session.scalar(
        select(AnalysisCorrection)
        .where(AnalysisCorrection.owner_id == owner_id, AnalysisCorrection.request_id == request_id)
        .with_for_update()
    )
    if prior is not None:
        if prior.request_sha256 != request_sha:
            raise PersistenceConflict("Correction request ID already has different content.")
        result = await Analyses(session).read(prior.result_analysis_id, owner_id=owner_id)
        action = next(
            (
                candidate
                for slide in result.slides
                for candidate in slide.actions
                if candidate.action.instance_id == instance_id
            ),
            None,
        )
        return ActionCorrectionOutcome(
            source_analysis_id=prior.source_analysis_id,
            result_analysis_id=prior.result_analysis_id,
            source_version=prior.source_version,
            result_version=prior.result_version,
            action_instance_id=instance_id,
            operation=inputs.operation,
            action_state=action.state if action else None,
            usable=action.usable if action else False,
        )
    latest = await Analyses(session).latest_for_deck(deck.id, owner_id=owner_id)
    if latest.id != record.id:
        raise PersistenceConflict("This analysis is no longer current. Refresh Review first.")
    if record.content_sha256 != inputs.expected_analysis_sha256:
        raise PersistenceConflict("The analysis changed. Refresh Review and retry.")
    if (
        not deck.source_revision
        or deck.source_revision != (record.provenance or {}).get("source_revision")
        or deck.source_sha256 != (record.provenance or {}).get("source_sha256")
    ):
        raise PersistenceConflict("The presentation source changed. Reprocess it before editing.")

    analyses = Analyses(session)
    source = await analyses.read(record.id, owner_id=owner_id)
    if not isinstance(source, (AnalysisResult, PowerPointAnalysisResult)) or source.inputs is None:
        raise PersistenceConflict("This analysis cannot be corrected.")
    original = next(
        (
            action.action
            for slide in source.slides
            for action in slide.actions
            if action.action.instance_id == instance_id
        ),
        None,
    )
    if original is None:
        raise RecordNotFound("The action is not part of this analysis.")
    if digest(original) != inputs.expected_action_sha256:
        raise PersistenceConflict("The action changed. Refresh Review and retry.")

    document = source.inputs.model_dump(mode="json")
    if isinstance(source.inputs, AnalysisInput):
        document["visual_qa_pending_instance_ids"] = list(
            source.inputs.visual_qa_pending_instance_ids
        )
    action_document = original.model_dump(mode="json")
    if inputs.operation == "set_region":
        assert inputs.region is not None
        if isinstance(source.inputs, AnalysisInput):
            source_slide = next(
                (
                    manifest_slide
                    for manifest_slide in source.inputs.manifest.slides
                    if manifest_slide.slide_id == original.slide_id
                ),
                None,
            )
            if source_slide is None:
                raise PersistenceConflict("The action source slide is no longer available.")
            if (
                inputs.region.x + inputs.region.width > source_slide.source.width
                or inputs.region.y + inputs.region.height > source_slide.source.height
            ):
                raise ValueError("The corrected region extends beyond its source slide.")
        action_document["params"]["region"] = inputs.region.model_dump(mode="json")
    for slide in document["slides"]:
        if slide["slide_id"] != original.slide_id:
            continue
        if inputs.operation == "disable":
            slide["actions"] = [
                item for item in slide["actions"] if item["instance_id"] != instance_id
            ]
        else:
            slide["actions"] = [
                action_document if item["instance_id"] == instance_id else item
                for item in slide["actions"]
            ]
    if isinstance(source.inputs, AnalysisInput):
        # The old source grounding identifies old geometry. Keep it only for unchanged actions.
        document["grounding"] = [
            item for item in document.get("grounding", []) if item["instance_id"] != instance_id
        ]
        if inputs.operation == "disable":
            document["visual_qa_pending_instance_ids"] = [
                item for item in document["visual_qa_pending_instance_ids"] if item != instance_id
            ]
    try:
        corrected_input = parse_analysis_input(document)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"The corrected region is invalid: {exc}") from None
    updated_action = (
        next(
            (
                action
                for slide in corrected_input.slides
                for action in slide.actions
                if action.instance_id == instance_id
            ),
            None,
        )
        if inputs.operation == "set_region"
        else None
    )
    changed = await analyses.create(
        owner_id=owner_id,
        deck_id=record.deck_id,
        version=record.version + 1,
        document=corrected_input,
    )
    await session.flush()

    # Carry evidence forward only for byte-identical actions under the same source,
    # validator, renderer and display profile. The new analysis binding is server-derived.
    for slide in source.slides:
        for action in slide.actions:
            if action.action.instance_id == instance_id or action.validation is None:
                continue
            cloned_validation = ValidationInput.model_validate(
                {
                    **action.validation.model_dump(mode="json"),
                    "validation_id": str(
                        uuid5(request_id, "preserved:" + action.action.instance_id)
                    ),
                    "analysis_id": str(changed.id),
                    "analysis_sha256": changed.content_sha256,
                }
            )
            existing_validation = await session.get(
                ActionValidation, action.validation.validation_id
            )
            evidence_source_id = (
                existing_validation.evidence_source_validation_id
                if existing_validation is not None
                and existing_validation.evidence_source_validation_id is not None
                else action.validation.validation_id
            )
            await analyses.record_validation(
                owner_id=owner_id,
                result=cloned_validation,
                evidence_source_validation_id=evidence_source_id,
            )

    result_action_hash = digest(updated_action) if updated_action is not None else None
    correction = AnalysisCorrection(
        owner_id=owner_id,
        deck_id=record.deck_id,
        source_analysis_id=record.id,
        result_analysis_id=changed.id,
        source_version=record.version,
        result_version=changed.version,
        action_instance_id=instance_id,
        operation=inputs.operation,
        source_action_sha256=digest(original),
        result_action_sha256=result_action_hash,
        request_id=request_id,
        request_sha256=request_sha,
        document=(
            {"region": inputs.region.model_dump(mode="json")}
            if inputs.operation == "set_region" and inputs.region is not None
            else {"disabled": True}
        ),
    )
    session.add(correction)
    await session.flush()

    current = await analyses.read(changed.id, owner_id=owner_id)
    action_result = next(
        (
            action
            for slide in current.slides
            for action in slide.actions
            if action.action.instance_id == instance_id
        ),
        None,
    )
    return ActionCorrectionOutcome(
        source_analysis_id=record.id,
        result_analysis_id=changed.id,
        source_version=record.version,
        result_version=changed.version,
        action_instance_id=instance_id,
        operation=inputs.operation,
        action_state=action_result.state if action_result else None,
        usable=action_result.usable if action_result else False,
    )
