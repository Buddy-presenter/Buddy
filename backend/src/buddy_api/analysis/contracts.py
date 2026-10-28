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


class ContentAction(Model):
    # Existing PDF action digests remain stable; the persistent record and response
    # envelope carry the format. Explicit mismatched inputs still fail validation.
    source_format: Literal["pdf"] = Field(default="pdf", exclude=True)
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
        checked = dict(value) if isinstance(value, dict) else value
        if isinstance(checked, dict) and checked.get("source_format") == "pdf":
            checked.pop("source_format")
        validate_catalog(checked)
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
    source_format: Literal["pdf"] = Field(default="pdf", exclude=True)
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
        if (
            len(set(self.visual_qa_pending_instance_ids))
            != len(self.visual_qa_pending_instance_ids)
            or not set(self.visual_qa_pending_instance_ids) <= action_ids
        ):
            raise ValueError("Visual QA pending references must name unique analysis actions.")
        if len(ids) > 1000 or len(self.model_dump_json().encode()) > 8 * 1024 * 1024:
            raise ValueError("Analysis exceeds the bounded record limit.")
        return self


class PowerPointParameters(Model):
    region: Region
    text: Annotated[str, Field(max_length=300)] = ""
    source_shape_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    target_text: Annotated[str, Field(max_length=300)] = ""


class PowerPointContentAction(Model):
    source_format: Literal["pptx"] = "pptx"
    version: Literal["powerpoint-2"] = "powerpoint-2"
    instance_id: Identifier
    purpose: Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")]
    deck_id: Identifier
    render_id: Identifier
    slide_id: Identifier
    type: Literal[
        "highlight_region",
        "draw_rectangle",
        "draw_oval",
        "draw_underline",
        "draw_arrow",
        "callout",
        "draw_line",
        "filled_rectangle",
        "filled_oval",
        "text_box",
        "draw_rounded_rectangle",
        "draw_diamond",
        "draw_triangle",
        "draw_hexagon",
        "draw_star",
    ]
    params: PowerPointParameters

    @model_validator(mode="after")
    def annotation(self) -> Self:
        if min(self.params.region.width, self.params.region.height) < 12:
            raise ValueError("PowerPoint annotations require a minimum 12-point region.")
        if self.type in {"callout", "text_box"}:
            if not self.params.text.strip() or any(
                ord(c) < 32 and c not in "\t\n" for c in self.params.text
            ):
                raise ValueError("A callout requires bounded annotation text.")
        elif self.params.text:
            raise ValueError("Only callouts and text boxes accept annotation text.")
        return self


class PowerPointPlane(Model):
    width: float = Field(gt=0, le=100000)
    height: float = Field(gt=0, le=100000)
    units: Literal["slide_points"] = "slide_points"
    origin: Literal["top_left"] = "top_left"


class PowerPointManifestSlide(Model):
    slide_id: Identifier
    native_slide_id: int = Field(strict=True, gt=0)
    source: PowerPointPlane


class PowerPointManifest(Model):
    source_format: Literal["pptx"] = "pptx"
    version: Literal["powerpoint-2"] = "powerpoint-2"
    deck_id: Identifier
    # This identifies extracted source geometry, not a fabricated rendered image.
    render_id: Identifier
    slides: Annotated[tuple[PowerPointManifestSlide, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({s.slide_id for s in self.slides}) != len(self.slides) or len(
            {s.native_slide_id for s in self.slides}
        ) != len(self.slides):
            raise ValueError("Duplicate PowerPoint slide identity.")
        return self


class PowerPointSlideOutput(Model):
    slide_id: Identifier
    summary: Summary
    actions: Annotated[tuple[PowerPointContentAction, ...], Field(max_length=40)] = ()


class PowerPointAnalysisInput(Model):
    schema_version: Literal["buddy.powerpoint.analysis.v2"] = "buddy.powerpoint.analysis.v2"
    source_format: Literal["pptx"] = "pptx"
    extraction_id: UUID
    extraction_schema_version: Literal["buddy.extraction.v1"] = "buddy.extraction.v1"
    extractor_version: Literal["1"] = "1"
    source_revision: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ]
    source_sha256: Digest
    office_safe: bool = False
    representation: Literal["bounded_ooxml_extraction"] = "bounded_ooxml_extraction"
    job_id: UUID
    catalog_version: Literal["powerpoint-2"] = "powerpoint-2"
    renderer_version: Literal["desktop-powerpoint-2"] = "desktop-powerpoint-2"
    display_profile: Literal["windows-powerpoint-2"] = "windows-powerpoint-2"
    validator_version: Literal["powerpoint-executor-2"] = "powerpoint-executor-2"
    manifest: PowerPointManifest
    presentation_summary: Summary
    slides: Annotated[tuple[PowerPointSlideOutput, ...], Field(min_length=1, max_length=100)]

    @property
    def visual_qa_pending_instance_ids(self) -> tuple[str, ...]:
        return ()

    @model_validator(mode="after")
    def references(self) -> Self:
        if [s.slide_id for s in self.slides] != [s.slide_id for s in self.manifest.slides]:
            raise ValueError("PowerPoint slides must match source order exactly.")
        ids: set[str] = set()
        for slide, source in zip(self.slides, self.manifest.slides, strict=True):
            for action in slide.actions:
                r = action.params.region
                if action.instance_id in ids or (
                    action.deck_id,
                    action.render_id,
                    action.slide_id,
                ) != (self.manifest.deck_id, self.manifest.render_id, slide.slide_id):
                    raise ValueError("Duplicate or stale PowerPoint action reference.")
                if r.x + r.width > source.source.width or r.y + r.height > source.source.height:
                    raise ValueError("PowerPoint action extends beyond its source slide.")
                ids.add(action.instance_id)
        if len(ids) > 100 or len(self.model_dump_json().encode()) > 8 * 1024 * 1024:
            raise ValueError("PowerPoint analysis exceeds its budget.")
        return self


def parse_analysis_input(value: object) -> AnalysisInput | PowerPointAnalysisInput:
    if isinstance(value, dict) and value.get("source_format") == "pptx":
        return PowerPointAnalysisInput.model_validate(value)
    return AnalysisInput.model_validate(value)


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
    source_format: Literal["pdf", "pptx"] = "pdf"
    runtime_version: Identifier | None = None
    schema_version: Literal["buddy.validation.v1"] = "buddy.validation.v1"
    validation_id: UUID
    analysis_id: UUID
    analysis_sha256: Digest
    instance_id: Identifier
    action_sha256: Digest
    validator_version: Identifier
    renderer_version: Identifier
    display_profile: Identifier
    state: ValidationState
    reasons: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")], ...],
        Field(max_length=20),
    ] = ()
    evidence: Annotated[tuple[EvidenceReference, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.source_format == "pptx" and self.state == "validated" and not self.runtime_version:
            raise ValueError("PowerPoint validation requires the actual Office runtime version.")
        if self.state == "validated" and (not self.evidence or self.reasons):
            raise ValueError("Validated results need evidence and no failure reasons.")
        if self.state in {"rejected", "needs-review"} and not self.reasons:
            raise ValueError("Unusable results need reasons.")
        if self.state == "candidate" and (self.evidence or self.reasons):
            raise ValueError("Candidates do not claim validation evidence.")
        if len({e.id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence references.")
        return self


class ActionResult(Model):
    action: ContentAction
    state: ValidationState
    usable: bool
    validation: ValidationInput | None


class SlideResult(Model):
    slide_id: Identifier
    summary: Summary
    actions: tuple[ActionResult, ...]


class AnalysisResult(Model):
    source_format: Literal["pdf"] = "pdf"
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


class PowerPointActionResult(Model):
    action: PowerPointContentAction
    state: ValidationState
    usable: bool
    validation: ValidationInput | None


class PowerPointSlideResult(Model):
    slide_id: Identifier
    summary: Summary
    actions: tuple[PowerPointActionResult, ...]


class PowerPointAnalysisResult(Model):
    source_format: Literal["pptx"] = "pptx"
    id: UUID
    deck_id: UUID
    version: int
    publication: Publication
    action_limit_reached: bool = False
    analysis_sha256: Digest | None
    inputs: PowerPointAnalysisInput | None
    slides: tuple[PowerPointSlideResult, ...]

    @property
    def visual_qa_pending_instance_ids(self) -> tuple[str, ...]:
        return ()


def publication(
    summary: Summary, slides: tuple[SlideResult, ...] | tuple[PowerPointSlideResult, ...]
) -> Publication:
    if summary.status != "available" or any(s.summary.status != "available" for s in slides):
        return "partial"
    if any(a.state == "candidate" for s in slides for a in s.actions):
        return "awaiting_validation"
    return "published"
