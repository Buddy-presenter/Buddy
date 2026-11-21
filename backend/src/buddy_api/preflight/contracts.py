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
