"""Requests select saved actions; clients can never submit validation verdicts."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from buddy_api.analysis.contracts import Identifier, Model


class PreflightRequest(Model):
    instance_id: Identifier
    profile: Literal["wide-1x", "wide-2x"] = "wide-1x"
    correct: bool = True
    runtime_platform: Literal["win32", "darwin"] = "win32"
    viewport: tuple[
        Annotated[int, Field(ge=320, le=4096, strict=True)],
        Annotated[int, Field(ge=320, le=4096, strict=True)],
    ] = (1280, 720)


class PresentationRuntime(Model):
    platform: Literal["win32", "darwin"]
    renderer_version: Identifier
    display_profile: Literal["wide-1x", "wide-2x"]


class PreflightReport(Model):
    id: UUID
    analysis_id: UUID
    instance_id: Identifier
    phase: Literal["queued", "running", "complete", "failed", "stale"]
    attempt: int = Field(ge=0, le=3)
    state: Literal["pending", "validated", "rejected", "unavailable"]
    reasons: list[str] = Field(max_length=20)
    corrected: bool = False
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=7)


class PreflightResponse(Model):
    data: PreflightReport
    request_id: str


# Base64 PNG, at most ~3.75 MB decoded (the model's image input limit).
Png = Annotated[str, Field(min_length=12, max_length=5_000_000)]


class PreflightSnapshotResponse(Model):
    """What the owner's desktop needs to run one PowerPoint check locally."""

    data: dict[str, Any]
    request_id: str


class PreflightCapture(Model):
    sequence: int = Field(ge=1, le=3, strict=True)
    target: dict[str, Any]
    before: Png
    after: Png


class PreflightVerdictResponse(Model):
    # None when the judge was unavailable; the attempt then stays unavailable.
    data: dict[str, Any] | None
    request_id: str


class PreflightResult(Model):
    report: dict[str, Any]
    images: dict[Annotated[str, Field(pattern=r"^attempt-[0-2]-(before|after)_image$")], Png] = (
        Field(max_length=6)
    )


class PreflightFailure(Model):
    reason: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")]
