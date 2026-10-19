"""Operator-controlled limits; queue identifiers never come from API clients."""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    queue_url: str = Field(
        pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/[A-Za-z0-9_-]+$"
    )
    region: str = Field(pattern=r"^[a-z]{2}-[a-z-]+-\d$")
    owner_quota: int = Field(default=4, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=5)
    lease_seconds: int = Field(default=45, ge=30, le=120)
    runtime_seconds: int = Field(default=120, ge=90, le=180)

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> "WorkerSettings | None":
        if not values.get("BUDDY_PROCESSING_QUEUE_URL"):
            return None
        return cls.model_validate(
            {
                "queue_url": values["BUDDY_PROCESSING_QUEUE_URL"],
                "region": values["BUDDY_AWS_REGION"],
                **{
                    name: values["BUDDY_WORKER_" + name.upper()]
                    for name in (
                        "owner_quota",
                        "concurrency",
                        "max_attempts",
                        "lease_seconds",
                        "runtime_seconds",
                    )
                    if "BUDDY_WORKER_" + name.upper() in values
                },
            }
        )
