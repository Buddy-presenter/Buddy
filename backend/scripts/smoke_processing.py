"""Opt-in real staging S3/SQS worker smoke; isolated test schema and disposable queues."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import boto3
from alembic import command
from sqlalchemy.schema import CreateSchema, DropSchema

from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.models import ProcessingJob
from buddy_api.persistence.settings import DatabaseSettings
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.queue import Queue
from buddy_api.processing.service import Processor
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.storage import SourceStorage


async def verify(args):
    if (
        boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
        != args.account
    ):
        raise RuntimeError("Unexpected AWS account")
    url = os.environ["BUDDY_TEST_DATABASE_URL"]
    admin = DatabaseSettings(url)
    if not admin.connection_url().database.endswith("_test"):
        raise ValueError("Use a disposable *_test database, not the application database")
    schema = "buddy_worker_smoke_" + uuid4().hex
    settings = DatabaseSettings(url, schema)
    engine = create_engine(admin)
    sqs = boto3.client("sqs", region_name=args.region)
    uploads = UploadSettings(bucket=args.bucket, region=args.region, kms_key=args.kms_key)
    storage, queue, db = SourceStorage(uploads), None, None
    queues, keys, processes = [], [], []
    report = {
        "account": args.account,
        "region": args.region,
        "database": "isolated local test schema",
        "live_staging_database": False,
        "checks": [],
    }
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            schema_created = True
        migrator = create_engine(settings)
        try:
            async with migrator.connect() as connection:
                await connection.run_sync(
                    lambda sync: command.upgrade(migration_config(settings, sync), "head")
                )
        finally:
            await migrator.dispose()
        name = "buddy-staging-worker-smoke-" + uuid4().hex[:12]
        dead = sqs.create_queue(
            QueueName=name + "-dead",
            Attributes={"SqsManagedSseEnabled": "true", "MessageRetentionPeriod": "3600"},
        )["QueueUrl"]
        queues.append(dead)
        arn = sqs.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])["Attributes"][
            "QueueArn"
        ]
        primary = sqs.create_queue(
            QueueName=name,
            Attributes={
                "SqsManagedSseEnabled": "true",
                "VisibilityTimeout": "60",
                "MessageRetentionPeriod": "3600",
                "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": "3"}),
            },
        )["QueueUrl"]
        queues.append(primary)
        worker_settings = WorkerSettings(queue_url=primary, region=args.region)
        queue, db = Queue(worker_settings), Database(settings)
        processor = Processor(db, storage, worker_settings, queue)
        fixture = Path(args.pdf)
        for index, content in enumerate(
            (fixture.read_bytes(), b"Malformed PDF fixture, no personal data")
        ):
            owner_id, identifier, revision = uuid4(), uuid4(), str(uuid4())
            key = f"sources/{owner_id}/{identifier}/{revision}/original.pdf"
            keys.append(key)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "source.pdf"
                path.write_bytes(content)
                digest = hashlib.sha256(content).hexdigest()
                version = storage.put(
                    key,
                    path,
                    size=len(content),
                    sha256=digest,
                    revision=revision,
                    content_type="application/pdf",
                )
            async with db.transaction() as repos:
                owner = await repos.users.create("worker-smoke-" + str(owner_id))
                deck = await repos.decks.create(
                    owner_id=owner.id, title="Disposable worker smoke", source_format="pdf"
                )
                deck.source_key, deck.source_revision, deck.source_version_id = (
                    key,
                    revision,
                    version,
                )
                deck.source_size_bytes, deck.source_sha256 = len(content), digest
            async with db.sessions() as session, session.begin():
                job = await store.schedule(session, deck)
                if index == 0:
                    from datetime import timedelta

                    from buddy_api.processing.contracts import Message

                    claimed = await store.claim(
                        session, Message(job_id=job.id, generation=0), worker_settings
                    )
                    claimed.lease_until = store.now() - timedelta(seconds=1)
            await processor.maintain()
            with tempfile.TemporaryDirectory(prefix="buddy-worker-smoke-runtime-") as runtime:
                env = {
                    **os.environ,
                    "BUDDY_ENVIRONMENT": "test",
                    "BUDDY_DATABASE_URL": url,
                    "BUDDY_DATABASE_SCHEMA": schema,
                    "BUDDY_UPLOADS_ENABLED": "true",
                    "BUDDY_DATA_BUCKET": args.bucket,
                    "BUDDY_DATA_KMS_KEY_ARN": args.kms_key,
                    "BUDDY_AWS_REGION": args.region,
                    "BUDDY_PROCESSING_QUEUE_URL": primary,
                    "TMPDIR": runtime,
                    "TMP": runtime,
                    "TEMP": runtime,
                }
                # Reconciliation of an expired job schedules a bounded delayed retry.
                if index == 0:
                    await asyncio.sleep(3)
                    await processor.maintain()
                log_path = Path(runtime) / "worker.log"
                log = log_path.open("wb")
                process = subprocess.Popen(
                    [sys.executable, "-m", "buddy_api.processing"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                )
                log.close()
                processes.append(process)
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    async with db.sessions() as session:
                        current = await session.get(ProcessingJob, job.id)
                    expected = "metadata_ready" if index == 0 else "dead_letter"
                    if current.status == expected:
                        break
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Real worker process exited: " + log_path.read_text(encoding="utf-8")
                        )
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("Real staging worker smoke timed out")
                health = subprocess.run(
                    [sys.executable, "-m", "buddy_api.processing", "--health"],
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                if health.returncode:
                    raise RuntimeError("Real worker health check failed")
                process.terminate()
                process.wait(timeout=130)
                if index == 0:
                    assert current.attempts == 2 and current.extraction_id is not None
                    async with db.transaction() as repos:
                        result = await repos.extractions.get(
                            current.extraction_id, owner_id=owner.id
                        )
                        assert result.document["source_sha256"] == digest
                        assert len(result.document["slides"]) > 0
                        assert await repos.analyses.list(owner_id=owner.id) == []
                else:
                    assert current.error_code == "pdf_inspection_failed"
                report["checks"].append(
                    "real_pdf_source_pinning_and_expired_lease_recovery"
                    if index == 0
                    else "malformed_pdf_dead_letter"
                )
        report["checks"].append("real_worker_startup_and_health")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
        if db:
            await db.close()
        for key in keys:
            storage.remove(key)
        for queue_url in reversed(queues):
            sqs.delete_queue(QueueUrl=queue_url)
        storage.close()
        if queue:
            queue.close()
        if schema_created:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()
    report["cleanup"] = "test objects (all versions), queues, schema and processes removed"
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kms-key", required=True)
    parser.add_argument("--pdf", required=True)
    args = parser.parse_args()
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
