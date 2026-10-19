"""SQS boundary with bounded transport; message bodies contain identifiers only."""

from typing import Any

from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message


class Queue:
    def __init__(self, settings: WorkerSettings):
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        self.settings = settings
        self.client: Any = boto3.client(
            "sqs",
            region_name=settings.region,
            config=Config(
                connect_timeout=3,
                read_timeout=25,
                retries={"total_max_attempts": 1},
            ),
        )

    def publish(self, message: Message) -> None:
        self.client.send_message(
            QueueUrl=self.settings.queue_url, MessageBody=message.model_dump_json()
        )

    def receive(self) -> list[dict[str, Any]]:
        return list(
            self.client.receive_message(
                QueueUrl=self.settings.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=10,
                # Worker attempts are bounded at fifteen minutes and renew the DB lease.
                # Matching SQS visibility prevents redelivery during provider/validator work.
                VisibilityTimeout=self.settings.runtime_seconds,
            ).get("Messages", [])
        )

    def acknowledge(self, receipt: str) -> None:
        self.client.delete_message(QueueUrl=self.settings.queue_url, ReceiptHandle=receipt)

    def check(self) -> None:
        self.client.get_queue_attributes(
            QueueUrl=self.settings.queue_url, AttributeNames=["QueueArn"]
        )

    def close(self) -> None:
        self.client.close()
