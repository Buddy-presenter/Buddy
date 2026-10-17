"""Bounded uploads configured by the API environment, never by the desktop."""

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class UploadSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z-]+-\d$")
    kms_key: str = Field(pattern=r"^arn:aws:kms:[a-z0-9-]+:\d{12}:key/[a-f0-9-]{36}$")
    max_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    expanded_bytes: int = Field(default=256 * 1024 * 1024, ge=1, le=256 * 1024 * 1024)
    max_slides: int = Field(default=500, ge=1, le=500)
    quota_bytes: int = Field(default=1024 * 1024 * 1024, ge=1, le=1024**4)
    owner_concurrency: int = Field(default=2, ge=1, le=8)
    global_concurrency: int = Field(default=8, ge=1, le=64)
    seconds: float = Field(default=120, ge=5, le=300)
    reservation_seconds: int = Field(default=600, ge=600, le=3600)

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> "UploadSettings | None":
        enabled = values.get("BUDDY_UPLOADS_ENABLED", "false")
        if enabled not in {"true", "false"}:
            raise ValueError("BUDDY_UPLOADS_ENABLED must be true or false.")
        if enabled == "false":
            return None
        try:
            numeric = {}
            for name in (
                "max_bytes",
                "expanded_bytes",
                "max_slides",
                "quota_bytes",
                "owner_concurrency",
                "global_concurrency",
                "seconds",
                "reservation_seconds",
            ):
                key = "BUDDY_UPLOAD_" + name.upper()
                if key in values:
                    if not re.fullmatch(r"\d+(?:\.\d+)?", values[key]):
                        raise ValueError
                    numeric[name] = values[key]
            return cls.model_validate(
                {
                    "bucket": values["BUDDY_DATA_BUCKET"],
                    "region": values["BUDDY_AWS_REGION"],
                    "kms_key": values["BUDDY_DATA_KMS_KEY_ARN"],
                    **numeric,
                }
            )
        except (KeyError, ValueError, ValidationError):
            raise ValueError("Invalid upload limits or private storage configuration.") from None
