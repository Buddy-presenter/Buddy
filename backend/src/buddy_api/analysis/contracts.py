"""Bounded three-output contracts and input/profile-specific validation evidence."""

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from buddy_api.analysis.catalog import validate_catalog

type Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Text = Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
type ValidationState = Literal["candidate", "validated", "rejected", "needs-review"]
type ValidationKind = Literal["execution", "visual"]
type GroundingMethod = Literal[
    "pdf_glyphs",
    "pdf_words",
    "ocr_words",
    "pdf_visual_geometry",
    "visual_hint",
    "pdf_candidate_line",
]
type Publication = Literal["legacy", "partial", "awaiting_validation", "published"]
MAX_GENERATED_ACTIONS_PER_DECK = 40


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Summary(Model):
    status: Literal["pending", "available", "unavailable"]
    text: Text | None = None
    reason: Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "available":
            if self.text is None or self.reason is not None:
                raise ValueError("Available summaries require text only.")
        elif self.text is not None or (self.status == "unavailable" and self.reason is None):
            raise ValueError("Unavailable summaries require a reason and no invented text.")
        return self


class Region(Model):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class RegionParameters(Model):
    region: Region


class GroundingTrace(Model):
    version: Literal["pdf-source-grounding-1"] = "pdf-source-grounding-1"
    method: GroundingMethod
    target_text: Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")] | None
    target_id: str | None = Field(
        default=None, max_length=128, exclude_if=lambda value: value is None
    )
    model_region: Region
    resolved_region: Region

    @model_validator(mode="after")
    def target_matches_method(self) -> Self:
        text_method = self.method in {"pdf_glyphs", "pdf_words", "ocr_words", "pdf_candidate_line"}
        if text_method != (self.target_text is not None):
            raise ValueError("Text grounding requires a quoted target; visual grounding has none.")
        if (self.method == "pdf_candidate_line") != (self.target_id is not None):
            raise ValueError("Candidate grounding requires its source-measured target ID.")
        return self


class ActionGrounding(Model):
    instance_id: Identifier
    action_sha256: Digest
    trace: GroundingTrace


class ContentAction(Model):
    version: Literal["1.1"]
    instance_id: Identifier
    purpose: Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")]
    deck_id: Identifier
    render_id: Identifier
    slide_id: Identifier
    type: Literal["zoom_region", "highlight_region"]
    params: RegionParameters

    @model_validator(mode="before")
    @classmethod
    def catalog(cls, value: object) -> object:
        validate_catalog(value)
        return value


class SourcePlane(Model):
    width: int = Field(strict=True, ge=1, le=16384)
    height: int = Field(strict=True, ge=1, le=16384)
    units: Literal["source_pixels"]
    origin: Literal["top_left"]


class Raster(Model):
    path: Annotated[
        str, Field(max_length=240, pattern=r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.png$")
    ]
    width: int = Field(strict=True, ge=1, le=8192)
    height: int = Field(strict=True, ge=1, le=8192)
    transform: Literal["scale"]
    sha256: Digest


class ManifestSlide(Model):
    slide_id: Identifier
    source: SourcePlane
    raster: Raster

    @model_validator(mode="after")
    def geometry(self) -> Self:
        if self.source.width * self.raster.height != self.source.height * self.raster.width:
            raise ValueError("Source and raster aspect ratios differ.")
        if self.raster.width * self.raster.height > 16777216:
            raise ValueError("Raster pixel limit exceeded.")
        return self


class Manifest(Model):
    version: Literal["1.0"]
    deck_id: Identifier
    render_id: Identifier
    slides: Annotated[tuple[ManifestSlide, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="before")
    @classmethod
    def catalog(cls, value: object) -> object:
        validate_catalog(value, manifest=True)
        return value

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({s.slide_id for s in self.slides}) != len(self.slides):
            raise ValueError("Duplicate manifest slide IDs.")
        return self


class SlideOutput(Model):
    slide_id: Identifier
    summary: Summary
    actions: Annotated[tuple[ContentAction, ...], Field(max_length=100)] = ()


class AnalysisInput(Model):
    schema_version: Literal["buddy.analysis.v1"] = "buddy.analysis.v1"
    extraction_id: UUID
    extraction_schema_version: Literal["buddy.extraction.v1"] = "buddy.extraction.v1"
    extractor_version: Literal["1"] = "1"
    source_revision: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ]
    source_sha256: Digest
    job_id: UUID
    catalog_version: Literal["1.1"] = "1.1"
    renderer_version: Identifier
    display_profile: Identifier
    validator_version: Identifier
    manifest: Manifest
    # Omit empty metadata to preserve the hashes of historical analysis documents.
    grounding: Annotated[
        tuple[ActionGrounding, ...], Field(max_length=40, exclude_if=lambda value: not value)
    ] = ()
    # Generated image/graphic subregions have no finer PDF object geometry. They remain
    # candidates until the independent visual QA workflow reviews their framing.
    visual_qa_pending_instance_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=40, exclude=True)
    ] = ()
    presentation_summary: Summary
    slides: Annotated[tuple[SlideOutput, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def references(self) -> Self:
        if [s.slide_id for s in self.slides] != [s.slide_id for s in self.manifest.slides]:
            raise ValueError("Every manifest slide must occur exactly once in order.")
        ids: set[str] = set()
        for slide, _rendered in zip(self.slides, self.manifest.slides, strict=True):
            for action in slide.actions:
                if action.instance_id in ids:
                    raise ValueError("Duplicate action instance ID.")
                ids.add(action.instance_id)
                if (action.deck_id, action.render_id, action.slide_id) != (
                    self.manifest.deck_id,
                    self.manifest.render_id,
                    slide.slide_id,
                ):
                    raise ValueError("Stale action references.")
                # Keep bounded model suggestions even when their geometry is unsafe.
                # The isolated executor must see and reject them so the published
                # analysis preserves the suggestion together with its reason/evidence.
        action_ids = {action.instance_id for slide in self.slides for action in slide.actions}
        actions_by_id = {
            action.instance_id: action for slide in self.slides for action in slide.actions
        }
        if len({item.instance_id for item in self.grounding}) != len(self.grounding):
            raise ValueError("Duplicate grounding action references.")
        for item in self.grounding:
            grounded_action = actions_by_id.get(item.instance_id)
            if (
                grounded_action is None
                or item.action_sha256 != digest(grounded_action)
                or item.trace.resolved_region != grounded_action.params.region
            ):
                raise ValueError("Grounding does not match the immutable action geometry/hash.")
            if (
                item.trace.method == "visual_hint"
                and item.instance_id not in self.visual_qa_pending_instance_ids
            ):
                raise ValueError("Unresolved visual hints must remain pending visual QA.")
        if (
            len(set(self.visual_qa_pending_instance_ids))
            != len(self.visual_qa_pending_instance_ids)
            or not set(self.visual_qa_pending_instance_ids) <= action_ids
        ):
            raise ValueError("Visual QA pending references must name unique analysis actions.")
        if len(ids) > 1000 or len(self.model_dump_json().encode()) > 8 * 1024 * 1024:
            raise ValueError("Analysis exceeds the bounded record limit.")
        return self


def digest(value: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class EvidenceReference(Model):
    id: UUID
    kind: Literal["executor_result", "before_image", "after_image", "report"]
    sha256: Digest


class ValidationInput(Model):
    schema_version: Literal["buddy.validation.v1"] = "buddy.validation.v1"
    validation_id: UUID
    analysis_id: UUID
    analysis_sha256: Digest
    instance_id: Identifier
    action_sha256: Digest
    validator_version: Identifier
    renderer_version: Identifier
    display_profile: Identifier
    validation_kind: ValidationKind = "execution"
    semantic_verified: bool = False
    state: ValidationState
    reasons: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")], ...],
        Field(max_length=20),
    ] = ()
    evidence: Annotated[tuple[EvidenceReference, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.state == "validated" and (not self.evidence or self.reasons):
            raise ValueError("Validated results need evidence and no failure reasons.")
        if self.state in {"rejected", "needs-review"} and not self.reasons:
            raise ValueError("Unusable results need reasons.")
        if self.state == "candidate" and (self.evidence or self.reasons):
            raise ValueError("Candidates do not claim validation evidence.")
        if self.semantic_verified and not (
            self.validation_kind == "visual" and self.state == "validated"
        ):
            raise ValueError("Semantic verification requires a validated visual review.")
        if (
            self.validation_kind == "visual"
            and self.state == "validated"
            and not self.semantic_verified
        ):
            raise ValueError("Validated visual review must verify the intended target.")
        if len({e.id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence references.")
        return self


class ActionResult(Model):
    action: ContentAction
    state: ValidationState
    usable: bool
    semantic_verified: bool = False
    validation: ValidationInput | None


class SlideResult(Model):
    slide_id: Identifier
    summary: Summary
    actions: tuple[ActionResult, ...]


class AnalysisResult(Model):
    id: UUID
    deck_id: UUID
    version: int
    publication: Publication
    action_limit_reached: bool = False
    analysis_sha256: Digest | None
    inputs: AnalysisInput | None
    slides: tuple[SlideResult, ...]
    # Worker-only handoff for the separate visual QA/correction workflow.
    visual_qa_pending_instance_ids: Annotated[
        tuple[Identifier, ...], Field(max_length=40, exclude=True)
    ] = ()


def publication(summary: Summary, slides: tuple[SlideResult, ...]) -> Publication:
    if summary.status != "available" or any(s.summary.status != "available" for s in slides):
        return "partial"
    if any(a.state == "candidate" for s in slides for a in s.actions):
        return "awaiting_validation"
    return "published"
