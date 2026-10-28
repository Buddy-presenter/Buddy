"""Separate PowerPoint process contract; never a PDF validation profile."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from buddy_api.analysis.contracts import (
    Digest,
    Identifier,
    Model,
    PowerPointAnalysisInput,
    PowerPointContentAction,
)


class PowerPointValidationRequest(Model):
    source_format: Literal["pptx"] = "pptx"
    action: dict[str, Any]
    profile: Literal["windows-powerpoint-2"] = "windows-powerpoint-2"
    slide_number: int = Field(strict=True, ge=1, le=100)
    native_slide_id: int = Field(strict=True, gt=0)
    slide_width: float = Field(gt=0, le=100000)
    slide_height: float = Field(gt=0, le=100000)

    @model_validator(mode="after")
    def action_contract(self) -> "PowerPointValidationRequest":
        PowerPointContentAction.model_validate(self.action)
        return self


def powerpoint_validation_request(action: Any, inputs: Any) -> PowerPointValidationRequest:
    if not isinstance(inputs, PowerPointAnalysisInput) or not isinstance(
        action, PowerPointContentAction
    ):
        raise ValueError("A matching PowerPoint analysis is required.")
    for index, slide in enumerate(inputs.manifest.slides):
        if slide.slide_id == action.slide_id:
            return PowerPointValidationRequest(
                action=action.model_dump(mode="json"),
                slide_number=index + 1,
                native_slide_id=slide.native_slide_id,
                slide_width=slide.source.width,
                slide_height=slide.source.height,
            )
    raise ValueError("PowerPoint action slide is not in its source.")


class PowerPointProcessRequest(PowerPointValidationRequest):
    schema_version: Literal["buddy.powerpoint-validation-request.v1"] = (
        "buddy.powerpoint-validation-request.v1"
    )
    deck_id: UUID
    source_revision: Identifier
    source_sha256: Digest


class PowerPointProcessResult(Model):
    schema_version: Literal["buddy.powerpoint-validation-result.v1"] = (
        "buddy.powerpoint-validation-result.v1"
    )
    source_format: Literal["pptx"] = "pptx"
    state: Literal["validated", "rejected", "needs-review"]
    reasons: list[Identifier] = Field(max_length=20)
    semantic_verified: Literal[False] = False
    validator_version: Literal["powerpoint-executor-2"] = "powerpoint-executor-2"
    renderer_version: Literal["desktop-powerpoint-2"] = "desktop-powerpoint-2"
    profile: Literal["windows-powerpoint-2"] = "windows-powerpoint-2"
    runtime_platform: Literal["linux", "win32", "darwin"]
    office_version: Identifier | None = None
    source_revision: Identifier
    source_sha256: Digest
    action_sha256: Digest
    before_sha256: Digest | None = None
    after_sha256: Digest | None = None

    @model_validator(mode="after")
    def execution(self) -> "PowerPointProcessResult":
        if self.state == "validated":
            if (
                self.runtime_platform != "win32"
                or not self.office_version
                or self.reasons
                or not self.before_sha256
                or not self.after_sha256
                or self.before_sha256 == self.after_sha256
            ):
                raise ValueError("PowerPoint validation requires a real changed Office rendering.")
        elif not self.reasons:
            raise ValueError("Unavailable actions require a reason.")
        return self
