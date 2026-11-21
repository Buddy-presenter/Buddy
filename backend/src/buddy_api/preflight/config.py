"""Approved operator executable and provider policy, never taken from HTTP input."""

from typing import Literal

from pydantic import Field

from buddy_api.validation.config import ValidatorSettings


class PreflightSettings(ValidatorSettings):
    region: str = Field(default="eu-central-1", pattern=r"^[a-z]{2}-[a-z-]+-\d$")
    model: str = "eu.anthropic.claude-sonnet-4-6"
    deadline: int = Field(default=450, ge=60, le=450)
    profile: Literal["wide-1x", "wide-2x"] = "wide-1x"
    viewport_width: int = Field(default=1280, ge=320, le=4096)
    viewport_height: int = Field(default=720, ge=320, le=4096)
