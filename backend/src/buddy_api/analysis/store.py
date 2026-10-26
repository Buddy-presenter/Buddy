"""Trusted, owner-scoped writes and typed reads of immutable analysis revisions."""

from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from buddy_api.analysis.contracts import (
    MAX_GENERATED_ACTIONS_PER_DECK,
    ActionResult,
    AnalysisInput,
    AnalysisResult,
    ContentAction,
    ManifestSlide,
    PowerPointActionResult,
    PowerPointAnalysisInput,
    PowerPointAnalysisResult,
    PowerPointContentAction,
    PowerPointManifestSlide,
    PowerPointSlideOutput,
    PowerPointSlideResult,
    SlideOutput,
    SlideResult,
    Summary,
    ValidationInput,
    digest,
    parse_analysis_input,
    publication,
)
from buddy_api.extraction.contracts import Extraction
from buddy_api.persistence.errors import PersistenceConflict, PersistenceUnavailable, RecordNotFound
from buddy_api.persistence.models import (
    ActionValidation,
    AnalysisSlide,
    AnalysisVersion,
    Deck,
    DeckExtraction,
    ProcessingJob,
    SlideAction,
)


def summary(record: AnalysisVersion | AnalysisSlide) -> Summary:
    return Summary.model_validate(
        {
            "status": record.summary_status,
            "text": record.summary_text,
            "reason": record.summary_reason,
        }
    )


class Analyses:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, identifier: UUID, *, owner_id: UUID) -> AnalysisVersion:
        value = await self.session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.id == identifier, AnalysisVersion.owner_id == owner_id
            )
        )
        if value is None:
            raise RecordNotFound("Owned analysis was not found.")
        return value

    async def latest_for_deck(self, deck_id: UUID, *, owner_id: UUID) -> AnalysisVersion:
        """Return the newest immutable analysis revision for one owned deck."""
        value = await self.session.scalar(
            select(AnalysisVersion)
            .where(AnalysisVersion.deck_id == deck_id, AnalysisVersion.owner_id == owner_id)
            .order_by(AnalysisVersion.version.desc(), AnalysisVersion.id.desc())
            .limit(1)
        )
        if value is None:
            raise RecordNotFound("Owned deck analysis was not found.")
        return value

    async def list(self, *, owner_id: UUID, limit: int = 100) -> list[AnalysisVersion]:
        if not 1 <= limit <= 100:
            raise ValueError("Repository limit must be from 1 to 100.")
        return list(
            await self.session.scalars(
                select(AnalysisVersion)
                .where(AnalysisVersion.owner_id == owner_id)
                .order_by(AnalysisVersion.created_at, AnalysisVersion.id)
                .limit(limit)
            )
        )

    async def create(
        self,
        *,
        owner_id: UUID,
        deck_id: UUID,
        version: int,
        document: AnalysisInput | PowerPointAnalysisInput,
    ) -> AnalysisVersion:
        # Revalidate even model_construct/model_copy caller values; they bypass Pydantic validation.
        document_values = document.model_dump(mode="json")
        if isinstance(document, AnalysisInput):
            document_values["visual_qa_pending_instance_ids"] = list(
                document.visual_qa_pending_instance_ids
            )
        document = parse_analysis_input(document_values)
        visual_qa_pending = set(document.visual_qa_pending_instance_ids)
        if type(version) is not int or version < 1:
            raise ValueError("Analysis version must be positive.")
        deck = await self.session.scalar(
            select(Deck).where(Deck.id == deck_id, Deck.owner_id == owner_id).with_for_update()
        )
        if deck is None:
            raise RecordNotFound("Owned deck was not found.")
        extraction = await self.session.scalar(
            select(DeckExtraction).where(
                DeckExtraction.id == document.extraction_id,
                DeckExtraction.deck_id == deck_id,
                DeckExtraction.owner_id == owner_id,
            )
        )
        job = await self.session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.id == document.job_id,
                ProcessingJob.deck_id == deck_id,
                ProcessingJob.owner_id == owner_id,
            )
        )
        if extraction is None or job is None:
            raise RecordNotFound("Owned analysis inputs were not found.")
        extracted = Extraction.model_validate(extraction.document)
        if (
            document.source_format != deck.source_format
            or extracted.source_format != document.source_format
            or document.manifest.deck_id != str(deck_id)
            or document.source_revision != extraction.source_revision
            or document.source_sha256 != extraction.source_sha256
            or extracted.source_revision != extraction.source_revision
            or extracted.source_sha256 != extraction.source_sha256
            or document.extractor_version != extraction.extractor_version
            or [s.id for s in extracted.slides] != [s.slide_id for s in document.slides]
            or [s.index for s in extracted.slides] != list(range(len(extracted.slides)))
        ):
            raise PersistenceConflict("Analysis source/slide revision does not match extraction.")
        manifest_slides = cast(
            tuple[ManifestSlide | PowerPointManifestSlide, ...], document.manifest.slides
        )
        for source, rendered in zip(extracted.slides, manifest_slides, strict=True):
            # Extraction EMU and canonical source pixels are different planes, with equal aspect.
            if (
                abs(source.width * rendered.source.height - source.height * rendered.source.width)
                > 0.01
            ):
                raise PersistenceConflict("Render geometry does not match the extracted slide.")
        content_hash = digest(document)
        existing = await self.session.scalar(
            select(AnalysisVersion).where(
                AnalysisVersion.deck_id == deck_id, AnalysisVersion.version == version
            )
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.content_sha256 != content_hash:
                raise PersistenceConflict("Analysis revision already has different content.")
            return existing
        value = AnalysisVersion(
            id=uuid4(),
            source_format=document.source_format,
            owner_id=owner_id,
            deck_id=deck_id,
            version=version,
            job_id=document.job_id,
            extraction_id=document.extraction_id,
            provenance=document.model_dump(mode="json", exclude={"slides", "presentation_summary"}),
            content_sha256=content_hash,
            publication="partial",
            summary_status=document.presentation_summary.status,
            summary_text=document.presentation_summary.text,
            summary_reason=document.presentation_summary.reason,
        )
        self.session.add(value)
        await self.session.flush()
        document_slides = cast(tuple[SlideOutput | PowerPointSlideOutput, ...], document.slides)
        for position, output in enumerate(document_slides):
            slide = AnalysisSlide(
                id=uuid4(),
                owner_id=owner_id,
                deck_id=deck_id,
                analysis_id=value.id,
                slide_id=output.slide_id,
                position=position,
                summary_status=output.summary.status,
                summary_text=output.summary.text,
                summary_reason=output.summary.reason,
            )
            self.session.add(slide)
            await self.session.flush()
            content_actions = cast(
                tuple[ContentAction | PowerPointContentAction, ...], output.actions
            )
            for index, action in enumerate(content_actions):
                record = SlideAction(
                    id=uuid4(),
                    source_format=document.source_format,
                    owner_id=owner_id,
                    deck_id=deck_id,
                    analysis_id=value.id,
                    slide_record_id=slide.id,
                    instance_id=action.instance_id,
                    position=index,
                    catalog_version=action.version,
                    action_type=action.type,
                    purpose=action.purpose,
                    parameters=action.params.model_dump(mode="json"),
                    content_sha256=digest(action),
                    visual_qa_pending=action.instance_id in visual_qa_pending,
                )
                self.session.add(record)
                await self.session.flush()
                self.session.add(
                    ActionValidation(
                        owner_id=owner_id,
                        source_format=document.source_format,
                        deck_id=deck_id,
                        analysis_id=value.id,
                        action_id=record.id,
                        sequence=0,
                        state="candidate",
                        document=None,
                    )
                )
        await self.session.flush()
        result = await self.read(value.id, owner_id=owner_id)
        value.publication = result.publication
        await self.session.flush()
        return value

    async def read(
        self, identifier: UUID, *, owner_id: UUID
    ) -> AnalysisResult | PowerPointAnalysisResult:
        value = await self.get(identifier, owner_id=owner_id)
        if value.publication == "legacy":
            if value.source_format == "pptx":
                return PowerPointAnalysisResult(
                    id=value.id,
                    deck_id=value.deck_id,
                    version=value.version,
                    publication="legacy",
                    analysis_sha256=None,
                    inputs=None,
                    slides=(),
                )
            return AnalysisResult(
                id=value.id,
                deck_id=value.deck_id,
                version=value.version,
                publication="legacy",
                analysis_sha256=None,
                inputs=None,
                slides=(),
            )
        try:
            return await self._read(value, owner_id)
        except (ValidationError, ValueError, KeyError, TypeError):
            raise PersistenceUnavailable("Stored analysis contract is unavailable.") from None

    async def _read(
        self, value: AnalysisVersion, owner_id: UUID
    ) -> AnalysisResult | PowerPointAnalysisResult:
        slides = list(
            await self.session.scalars(
                select(AnalysisSlide)
                .where(AnalysisSlide.analysis_id == value.id, AnalysisSlide.owner_id == owner_id)
                .order_by(AnalysisSlide.position)
            )
        )
        actions = list(
            await self.session.scalars(
                select(SlideAction)
                .where(SlideAction.analysis_id == value.id, SlideAction.owner_id == owner_id)
                .order_by(SlideAction.position)
            )
        )
        validations = list(
            await self.session.scalars(
                select(ActionValidation)
                .ext(distinct_on(ActionValidation.action_id))
                .where(
                    ActionValidation.analysis_id == value.id, ActionValidation.owner_id == owner_id
                )
                .order_by(ActionValidation.action_id, ActionValidation.sequence.desc())
            )
        )
        latest = {v.action_id: v for v in validations}
        manifest = (value.provenance or {})["manifest"]
        visual_qa_pending = tuple(
            action.instance_id for action in actions if action.visual_qa_pending
        )
        results: list[Any] = []
        action_result_model: Any = (
            PowerPointActionResult if value.source_format == "pptx" else ActionResult
        )
        slide_result_model: Any = (
            PowerPointSlideResult if value.source_format == "pptx" else SlideResult
        )
        for slide in slides:
            output = []
            for action in actions:
                if action.slide_record_id != slide.id:
                    continue
                action_model = (
                    PowerPointContentAction if value.source_format == "pptx" else ContentAction
                )
                if action.source_format != value.source_format:
                    raise ValueError("Stored action format mismatch.")
                instance = action_model.model_validate(
                    {
                        "version": action.catalog_version,
                        "instance_id": action.instance_id,
                        "purpose": action.purpose,
                        "deck_id": str(value.deck_id),
                        "render_id": manifest["render_id"],
                        "slide_id": slide.slide_id,
                        "type": action.action_type,
                        "params": action.parameters,
                        **({"source_format": "pptx"} if value.source_format == "pptx" else {}),
                    }
                )
                stored = latest.get(action.id)
                evidence = (
                    ValidationInput.model_validate(stored.document)
                    if stored and stored.document
                    else None
                )
                if digest(instance) != action.content_sha256:
                    raise ValueError("Action digest mismatch.")
                if evidence is not None:
                    self._check_evidence(value, action, evidence)
                    if stored is None or stored.state != evidence.state:
                        raise ValueError("Validation state mismatch.")
                state = evidence.state if evidence else "candidate"
                output.append(
                    action_result_model(
                        action=instance,
                        state=state,
                        usable=state == "validated",
                        validation=evidence,
                    )
                )
            results.append(
                slide_result_model(
                    slide_id=slide.slide_id, summary=summary(slide), actions=tuple(output)
                )
            )
        inputs = parse_analysis_input(
            {
                **(value.provenance or {}),
                "presentation_summary": summary(value).model_dump(),
                "slides": [
                    {
                        "slide_id": s.slide_id,
                        "summary": s.summary.model_dump(),
                        "actions": [a.action.model_dump(mode="json") for a in s.actions],
                    }
                    for s in results
                ],
            }
        )
        if (
            inputs.source_format != value.source_format
            or digest(inputs) != value.content_sha256
            or (inputs.extraction_id != value.extraction_id or inputs.job_id != value.job_id)
        ):
            raise ValueError("Analysis digest/lineage mismatch.")
        if isinstance(inputs, PowerPointAnalysisInput):
            return PowerPointAnalysisResult(
                id=value.id,
                deck_id=value.deck_id,
                version=value.version,
                publication=publication(inputs.presentation_summary, tuple(results)),
                action_limit_reached=sum(len(s.actions) for s in results)
                == MAX_GENERATED_ACTIONS_PER_DECK,
                analysis_sha256=value.content_sha256,
                inputs=inputs,
                slides=tuple(results),
            )
        return AnalysisResult(
            id=value.id,
            deck_id=value.deck_id,
            version=value.version,
            publication=publication(inputs.presentation_summary, tuple(results)),
            action_limit_reached=sum(len(slide.actions) for slide in results)
            == MAX_GENERATED_ACTIONS_PER_DECK,
            analysis_sha256=value.content_sha256,
            inputs=inputs,
            slides=tuple(results),
            visual_qa_pending_instance_ids=visual_qa_pending,
        )

    @staticmethod
    def _check_evidence(
        value: AnalysisVersion, action: SlideAction, evidence: ValidationInput
    ) -> None:
        provenance = value.provenance or {}
        if (
            evidence.analysis_id != value.id
            or evidence.source_format != value.source_format
            or action.source_format != value.source_format
            or evidence.analysis_sha256 != value.content_sha256
            or evidence.instance_id != action.instance_id
            or evidence.action_sha256 != action.content_sha256
            or any(
                getattr(evidence, key) != provenance.get(key)
                for key in ("validator_version", "renderer_version", "display_profile")
            )
        ):
            raise PersistenceConflict(
                "Validation does not match the analysis/action/profile revision."
            )

    async def record_validation(
        self, *, owner_id: UUID, result: ValidationInput
    ) -> AnalysisResult | PowerPointAnalysisResult:
        """Trusted validator boundary only. There is intentionally no HTTP write endpoint."""
        result = ValidationInput.model_validate(result.model_dump(mode="json"))
        value = await self.session.scalar(
            select(AnalysisVersion)
            .where(AnalysisVersion.id == result.analysis_id, AnalysisVersion.owner_id == owner_id)
            .with_for_update()
        )
        if value is None:
            raise RecordNotFound("Owned analysis was not found.")
        action = await self.session.scalar(
            select(SlideAction).where(
                SlideAction.analysis_id == value.id,
                SlideAction.owner_id == owner_id,
                SlideAction.instance_id == result.instance_id,
            )
        )
        if action is None:
            raise RecordNotFound("Owned action was not found.")
        self._check_evidence(value, action, result)
        existing = await self.session.get(ActionValidation, result.validation_id)
        document = result.model_dump(mode="json")
        if existing is not None:
            if (
                existing.owner_id != owner_id
                or existing.action_id != action.id
                or existing.document != document
            ):
                raise PersistenceConflict("Validation ID already has different content.")
            return await self.read(value.id, owner_id=owner_id)
        latest = await self.session.scalar(
            select(ActionValidation)
            .where(ActionValidation.action_id == action.id)
            .order_by(ActionValidation.sequence.desc())
            .limit(1)
        )
        sequence = latest.sequence + 1 if latest else 0
        if sequence > 100:
            raise PersistenceConflict(
                "Validation history limit reached; create a new analysis revision."
            )
        self.session.add(
            ActionValidation(
                id=result.validation_id,
                source_format=result.source_format,
                owner_id=owner_id,
                deck_id=value.deck_id,
                analysis_id=value.id,
                action_id=action.id,
                sequence=sequence,
                state=result.state,
                document=document,
            )
        )
        await self.session.flush()
        current = await self.read(value.id, owner_id=owner_id)
        value.publication = current.publication
        await self.session.flush()
        return current
