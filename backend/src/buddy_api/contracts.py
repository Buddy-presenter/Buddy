"""Versioned wire models shared by routes and their generated OpenAPI schema."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthData(Contract):
    status: Literal["ok"] = "ok"


class HealthResponse(Contract):
    data: HealthData
    request_id: str


class LegacyHealthResponse(Contract):
    """Compatibility with the existing desktop HealthService."""

    status: Literal["ok"] = "ok"
    request_id: str


class DependencyStatus(Contract):
    name: str
    status: Literal["ready", "unavailable"]


class ReadyData(Contract):
    status: Literal["ready"] = "ready"
    dependencies: list[DependencyStatus] = Field(default_factory=list)


class ReadyResponse(Contract):
    data: ReadyData
    request_id: str


class VersionData(Contract):
    service: Literal["buddy-api"] = "buddy-api"
    version: str
    api_version: Literal["v1"] = "v1"


class VersionResponse(Contract):
    data: VersionData
    request_id: str


class ErrorDetail(Contract):
    location: str
    code: str


class ErrorData(Contract):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(Contract):
    error: ErrorData
    request_id: str
