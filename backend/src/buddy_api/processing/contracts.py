"""Versioned stage contracts; metadata is not provider analysis or review readiness."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from buddy_api.analysis.contracts import Manifest, PowerPointManifest
from buddy_api.extraction.contracts import Extraction

ACTIVE = ("queued", "retry_wait", "running")
TERMINAL = (
    "metadata_ready",
    "analysis_ready",
    "analysis_partial",
    "cancelled",
    "dead_letter",
    "failed",
)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["buddy.job.v1"] = "buddy.job.v1"
    job_id: UUID
    generation: int = Field(ge=0)


class MetadataOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal["pdf_metadata.v1"] = "pdf_metadata.v1"
    metadata: Extraction
    manifest: Manifest


class PowerPointMetadataOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal["pptx_metadata.v1"] = "pptx_metadata.v1"
    metadata: Extraction
    manifest: PowerPointManifest


class PDFSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_format: Literal["pdf"] = "pdf"
    key: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=1, le=64 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1, max_length=128)
    deck_id: UUID


class PowerPointSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_format: Literal["pptx"] = "pptx"
    key: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=1, le=64 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1, max_length=128)
    deck_id: UUID


class JobData(BaseModel):
    id: UUID
    deck_id: UUID
    source_revision: str | None
    status: str
    stage: str
    stage_state: str
    attempts: int
    progress: int
    error_code: str | None
    lease_until: datetime | None
    extraction_id: UUID | None
    analysis_ready: Literal[False] = False


class JobResponse(BaseModel):
    data: JobData
    request_id: str
