"""Versioned validator process and public result contracts; no Qt dependencies."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from buddy_api.analysis.contracts import Digest, Identifier, Model, Region
from buddy_api.validation.powerpoint import PowerPointProcessResult

type Profile = Literal["wide-1x", "standard-1x", "portrait-1x", "wide-2x"]
type Precision = Literal["standard", "line"]


class ValidationRequest(Model):
    action: dict[str, Any]
    profile: Profile = "wide-1x"

    @model_validator(mode="after")
    def bounded(self) -> "ValidationRequest":
        pending, visited = [(self.action, 0)], 0
        while pending:
            value, depth = pending.pop()
            visited += 1
            if depth > 10 or visited > 2000:
                raise ValueError("Action structure exceeds the validation input limit")
            if isinstance(value, dict):
                if len(value) > 16:
                    raise ValueError("Action object exceeds the validation input limit")
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                if len(value) > 100:
                    raise ValueError("Action array exceeds the validation input limit")
                pending.extend((child, depth + 1) for child in value)
        if len(json.dumps(self.action, allow_nan=False).encode()) > 8192:
            raise ValueError("Action exceeds the validation input limit")
        return self


class ProcessRequest(ValidationRequest):
    schema_version: Literal["buddy.action-validation-request.v1"] = (
        "buddy.action-validation-request.v1"
    )
    deck_id: UUID
    source_revision: Identifier
    source_sha256: Digest
    precision: Precision = "standard"
    duplicate_candidate: bool = False


class Snapshot(Model):
    slide_id: Identifier
    region: Region
    highlight: Region | None
    scale: float = Field(gt=0)
    offset_x: float
    offset_y: float
    width: int = Field(ge=1, le=1280)
    height: int = Field(ge=1, le=960)
    dpr: float = Field(gt=0, le=2)


class ProcessResult(Model):
    schema_version: Literal["buddy.action-validation-result.v1"] = (
        "buddy.action-validation-result.v1"
    )
    state: Literal["validated", "rejected", "needs-review"]
    reasons: list[Identifier] = Field(max_length=20)
    semantic_verified: Literal[False] = False
    validator_version: Literal["qt-executor-1"] = "qt-executor-1"
    renderer_version: Literal["pdfium-5.14.0-144dpi"] = "pdfium-5.14.0-144dpi"
    profile: Profile
    runtime_platform: Literal["linux", "win32", "darwin"]
    qt_version: Identifier = "6.12.0"
    source_revision: Identifier
    source_sha256: Digest
    action_sha256: Digest
    before: Snapshot | None = None
    after: Snapshot | None = None

    @model_validator(mode="after")
    def outcome(self) -> "ProcessResult":
        if self.state == "validated":
            if self.reasons or self.before is None or self.after is None:
                raise ValueError("Validation requires execution evidence")
        elif not self.reasons:
            raise ValueError("Unvalidated results require reasons")
        return self


class ArtifactReference(Model):
    id: UUID
    kind: Literal["before_image", "after_image", "report"]
    sha256: Digest
    size_bytes: int = Field(ge=1, le=16 * 1024 * 1024)


class StoredResult(Model):
    id: UUID
    deck_id: UUID
    result: ProcessResult
    evidence: list[ArtifactReference] = Field(max_length=3)


class PowerPointStoredResult(Model):
    id: UUID
    deck_id: UUID
    result: PowerPointProcessResult
    evidence: list[ArtifactReference] = Field(max_length=3)


class ValidationResponse(Model):
    data: StoredResult
    request_id: str


def action_digest(action: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(action, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
