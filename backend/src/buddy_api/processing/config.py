"""Operator-controlled limits; queue identifiers never come from API clients."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    queue_url: str = Field(
        pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/[A-Za-z0-9_-]+$"
    )
    region: str = Field(pattern=r"^[a-z]{2}-[a-z-]+-\d$")
    text_targeting: Literal["quote", "candidate_lines"] = "quote"
    owner_quota: int = Field(default=4, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=5)
    lease_seconds: int = Field(default=45, ge=30, le=120)
    runtime_seconds: int = Field(default=900, ge=300, le=900)
    queue_message_retention_seconds: int = Field(default=86400, ge=60, le=1209600)
    queue_redrive_max_receive_count: int = Field(default=3, ge=2, le=1000)

    @model_validator(mode="after")
    def validate_reconciliation_window(self) -> "WorkerSettings":
        if self.queue_message_retention_seconds < self.runtime_seconds * 2:
            raise ValueError(
                "Queue retention must allow at least one worker visibility interval "
                "before reconciliation."
            )
        return self

    @property
    def queue_reconciliation_seconds(self) -> int:
        """Re-publish before either source retention expiry or SQS redrive to its DLQ."""
        invalidation_horizon = min(
            self.queue_message_retention_seconds,
            self.runtime_seconds * self.queue_redrive_max_receive_count,
        )
        return invalidation_horizon - self.runtime_seconds

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
                        "text_targeting",
                        "concurrency",
                        "max_attempts",
                        "lease_seconds",
                        "runtime_seconds",
                        "queue_message_retention_seconds",
                        "queue_redrive_max_receive_count",
                    )
                    if "BUDDY_WORKER_" + name.upper() in values
                },
            }
        )
