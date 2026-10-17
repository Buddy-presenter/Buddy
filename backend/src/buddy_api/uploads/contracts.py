"""Public ingestion models expose opaque identities, never S3 paths or ownership overrides."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from buddy_api.contracts import ErrorData, ErrorResponse

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF_TYPE = "application/pdf"
SOURCE_TYPES = {"pptx": PPTX_TYPE, "pdf": PDF_TYPE}


class UploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str = Field(min_length=5, max_length=255)
    title: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(strict=True, ge=1, le=64 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: Literal[
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/pdf",
    ]

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            not value.lower().endswith((".pptx", ".pdf"))
            or any(c in value for c in '/\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or value.startswith(".")
        ):
            raise ValueError("Unsupported presentation filename")
        return value

    @property
    def source_format(self) -> str:
        return "pdf" if self.content_type == PDF_TYPE else "pptx"

    @model_validator(mode="after")
    def matching_format(self) -> "UploadMetadata":
        if not self.filename.lower().endswith("." + self.source_format):
            raise ValueError("Filename and content type must agree")
        return self

    @field_validator("title")
    @classmethod
    def safe_title(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid deck title")
        return value


class UploadedDeck(BaseModel):
    id: UUID
    title: str
    source_revision: str
    sha256: str
    size_bytes: int
    slide_count: int
    source_format: Literal["pptx", "pdf"] = "pptx"
    status: Literal["uploaded"] = "uploaded"


class UploadData(BaseModel):
    id: UUID
    status: Literal["pending", "receiving", "cleanup", "failed", "complete"]
    expires_at: datetime
    retryable: bool
    error_code: str | None = None
    deck: UploadedDeck | None = None


class UploadResponse(BaseModel):
    data: UploadData
    request_id: str


class UploadError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def body(self, request_id: str) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorData(code=self.code, message=self.message), request_id=request_id
        )
