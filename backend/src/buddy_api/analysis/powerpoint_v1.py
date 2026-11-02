"""Frozen v1 HTTP PowerPoint response models; internal processing uses the v2 models."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from buddy_api.analysis.contracts import (
    Digest,
    Identifier,
    Model,
    PowerPointManifest,
    PowerPointParameters,
    Publication,
    Summary,
    ValidationInput,
    ValidationState,
)


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
