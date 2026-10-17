"""Operator-owned validator executable and bounded runtime configuration."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ValidatorSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    python: str
    seconds: int = Field(default=60, ge=5, le=90)
    memory_mb: int = Field(default=768, ge=256, le=768)

    @field_validator("python")
    @classmethod
    def executable(cls, value: str) -> str:
        if not Path(value).is_absolute() or not Path(value).is_file():
            raise ValueError("Validator Python must be an installed absolute executable path")
        return value
