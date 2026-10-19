"""Operator-invoked ECS acceptance payload; never a deployed application entry point."""

import asyncio
import json
import os
import re
import ssl
import sys
import time
from datetime import timedelta

import asyncpg
import boto3
from alembic import command
from botocore.exceptions import ClientError
from sqlalchemy import select, text
from sqlalchemy.engine import URL

from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.models import JobOutbox, ProcessingJob
from buddy_api.persistence.settings import DatabaseSettings
from buddy_api.processing import store
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message
from buddy_api.processing.queue import Queue
from buddy_api.processing.service import Processor
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.storage import SourceStorage


def event(name, **values):
    print(json.dumps({"event": name, **values}), flush=True)


def validate(config):
    if not re.fullmatch(r"[a-f0-9]{12}", config["run"]):
        raise ValueError("Invalid acceptance run")
    if config["database"] != "buddy_worker_acceptance_" + config["run"] + "_test":
        raise ValueError("Not an isolated acceptance database")
    if config["db_role"] != "buddy_w_" + config["run"]:
        raise ValueError("Not an isolated acceptance role")
    if config["cluster"] != "buddy-staging":
        raise ValueError("Acceptance is staging-only")
    if config["prefix"] != "sources/acceptance/" + config["run"] + "/":
        raise ValueError("Not an isolated acceptance source prefix")


def secret(arn, region):
    return json.loads(
        boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=arn)[
            "SecretString"
        ]
    )


async def administrative(config):
    credentials = secret(config["admin_secret"], config["region"])
    return await asyncpg.connect(
        host=config["host"],
        database="postgres",
        user=credentials["username"],
        password=credentials["password"],
        ssl=ssl.create_default_context(),
        timeout=10,
        command_timeout=30,
    )


async def prepare(config):
    credentials = secret(config["runtime_secret"], config["region"])
    master = secret(config["admin_secret"], config["region"])
    connection = await administrative(config)
    marker = "buddy-worker-acceptance:" + config["run"]
    try:
        if await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", config["database"]
        ):
            raise RuntimeError("Acceptance database already exists")
        async with connection.transaction():
            sql = await connection.fetchval(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)",
                config["db_role"],
                credentials["password"],
            )
            await connection.execute(sql)
            await connection.execute(
                await connection.fetchval(
                    "SELECT format('COMMENT ON ROLE %I IS %L', $1::text, $2::text)",
                    config["db_role"],
                    marker,
                )
            )
        await connection.execute(f'CREATE DATABASE "{config["database"]}"')
        await connection.execute(
            await connection.fetchval(
                "SELECT format('COMMENT ON DATABASE %I IS %L', $1::text, $2::text)",
                config["database"],
                marker,
            )
        )
        await connection.execute(f'REVOKE ALL ON DATABASE "{config["database"]}" FROM PUBLIC')
        await connection.execute(
            f'GRANT CONNECT ON DATABASE "{config["database"]}" TO "{config["db_role"]}"'
        )
    finally:
        await connection.close()
    event("isolated_database_created")
    url = URL.create(
        "postgresql",
        username=master["username"],
        password=master["password"],
        host=config["host"],
        database=config["database"],
        query={"sslmode": "verify-full"},
    )
    settings = DatabaseSettings(url.render_as_string(hide_password=False), production=True)
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(migration_config(settings, sync), "head")
            )
        async with engine.begin() as connection:
            await connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{config["db_role"]}"'))
            await connection.execute(
                text(
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public "
                    f'TO "{config["db_role"]}"'
                )
            )
            await connection.execute(
                text(f'REVOKE INSERT,UPDATE,DELETE ON alembic_version FROM "{config["db_role"]}"')
            )
        database = Database(settings)
        try:
            async with database.transaction() as repos:
                owner = await repos.users.create("acceptance-" + config["run"])
                decks = {}
                for label, source in config["sources"].items():
                    deck = await repos.decks.create(
                        owner_id=owner.id, title=label, source_format="pdf"
                    )
                    for column, key in (
                        ("source_key", "key"),
                        ("source_revision", "revision"),
                        ("source_version_id", "version"),
                        ("source_size_bytes", "size"),
                        ("source_sha256", "sha256"),
                    ):
                        setattr(deck, column, source[key])
                    decks[label] = deck
            identifiers = {}
            async with database.sessions() as session, session.begin():
                for label, deck in decks.items():
                    job = await store.schedule(session, deck)
                    identifiers[label] = str(job.id)
                    job.not_before = store.now() + timedelta(minutes=20)
                    (await session.get(JobOutbox, job.id)).available_at = job.not_before
            return {
                "jobs": identifiers,
                "owner": str(owner.id),
                "checks": ["private_rds_tls", "isolated_migration_0009"],
            }
        finally:
            await database.close()
    finally:
        await engine.dispose()


async def cleanup(config):
    connection = await administrative(config)
    marker = "buddy-worker-acceptance:" + config["run"]
    try:
        record = await connection.fetchrow(
            "SELECT oid, shobj_description(oid, 'pg_database') AS marker "
            "FROM pg_database WHERE datname=$1",
            config["database"],
        )
        if record:
            if record["marker"] != marker:
                raise RuntimeError("Refusing to remove unowned database")
            await connection.execute(f'DROP DATABASE "{config["database"]}" WITH (FORCE)')
        record = await connection.fetchrow(
            "SELECT oid, shobj_description(oid, 'pg_authid') AS marker "
            "FROM pg_roles WHERE rolname=$1",
            config["db_role"],
        )
        if record:
            if record["marker"] != marker:
                raise RuntimeError("Refusing to remove unowned role")
            await connection.execute(f'DROP ROLE "{config["db_role"]}"')
        assert not await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", config["database"]
        )
        assert not await connection.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname=$1", config["db_role"]
        )
    finally:
        await connection.close()
    return {"database_removed": True, "database_role_removed": True}


async def permissions(config):
    settings = DatabaseSettings.from_environment(production=True)
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == config["db_role"]
            assert await connection.scalar(
                text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
            )
        for query in (
            "CREATE TABLE public.unapproved_table(id integer)",
            "DELETE FROM alembic_version WHERE false",
        ):
            denied = False
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(query))
            except Exception as exc:
                denied = "InsufficientPrivilege" in type(
                    getattr(exc, "orig", exc)
                ).__name__ or "permission denied" in str(exc)
            assert denied, "Runtime database role must reject schema/version writes"
    finally:
        await engine.dispose()
    uploads = UploadSettings.from_environment(os.environ)
    storage = SourceStorage(uploads)
    source = config["sources"]["good"]
    try:
        content = storage.read(
            source["key"],
            version=source["version"],
            size=source["size"],
            sha256=source["sha256"],
            source_format="pdf",
        )
        assert len(content) == source["size"]
        denied = 0
        for call in (
            lambda: storage.client().put_object(
                Bucket=config["bucket"], Key=config["prefix"] + "denied", Body=b"synthetic"
            ),
            lambda: storage.client().delete_object(
                Bucket=config["bucket"], Key=config["prefix"] + "denied"
            ),
            lambda: boto3.client("sqs", region_name=config["region"]).send_message(
                QueueUrl=config["queue"], MessageBody="denied"
            ),
            lambda: secret(config["admin_secret"], config["region"]),
        ):
            try:
                call()
            except ClientError as exc:
                assert exc.response["Error"]["Code"] in {
                    "AccessDenied",
                    "AccessDeniedException",
                    "KMS.AccessDeniedException",
                }
                denied += 1
        assert denied == 4, "Worker task role exceeded its privileges"
    finally:
        storage.close()
    return {
        "checks": [
            "runtime_role_tls",
            "runtime_ddl_and_version_writes_denied",
            "worker_version_pinned_s3_read",
            "worker_s3_write_delete_denied",
            "worker_queue_publish_denied",
            "worker_admin_secret_denied",
        ]
    }


async def exercise(config):
    from uuid import UUID

    database = Database(DatabaseSettings.from_environment(production=True))
    settings = WorkerSettings.from_environment(os.environ)
    storage, queue = SourceStorage(UploadSettings.from_environment(os.environ)), Queue(settings)
    processor = Processor(database, storage, settings, queue)
    identifiers = {label: UUID(value) for label, value in config["jobs"].items()}
    checks = []
    deadline = time.monotonic() + 540

    async def arm(label):
        async with database.sessions() as session, session.begin():
            job = await session.get(ProcessingJob, identifiers[label])
            job.not_before = store.now()
            (await session.get(JobOutbox, job.id)).available_at = store.now()

    async def wait(label, status):
        while time.monotonic() < deadline:
            await processor.maintain()
            async with database.sessions() as session:
                job = await session.get(ProcessingJob, identifiers[label])
            if job.status == status:
                return job
            if job.status in {"dead_letter", "metadata_ready"}:
                raise RuntimeError("Unexpected terminal state for " + label)
            await asyncio.sleep(0.05 if status == "running" else 1)
        raise TimeoutError("Acceptance attempt budget exhausted")

    try:
        await arm("good")
        await arm("bad")
        await processor.maintain()
        await asyncio.to_thread(queue.publish, Message(job_id=identifiers["good"], generation=0))
        first = await wait("good", "metadata_ready")
        assert first.attempts == 1
        bad = await wait("bad", "dead_letter")
        assert bad.error_code == "pdf_inspection_failed" and bad.extraction_id is None
        checks += [
            "real_sqs_duplicate_delivery",
            "real_pdf_inspection",
            "malformed_pdf_dead_letter",
        ]
        event("initial_processing_passed")
        await arm("restart")
        await wait("restart", "running")
        async with database.sessions() as session, session.begin():
            job = await session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.id == identifiers["restart"])
                .with_for_update()
            )
            assert job.status == "running" and job.attempts == 1
            # Hold the completion fence while ECS kills this two-second-stop drill task.
            ecs = boto3.client("ecs", region_name=config["region"])
            await asyncio.to_thread(
                ecs.stop_task,
                cluster=config["cluster"],
                task=config["fault_task"],
                reason="Isolated issue8 acceptance interruption",
            )
            await asyncio.to_thread(
                ecs.get_waiter("tasks_stopped").wait,
                cluster=config["cluster"],
                tasks=[config["fault_task"]],
                WaiterConfig={"Delay": 2, "MaxAttempts": 40},
            )
        event("fault_injected_waiting_for_replacement")
        recovered = await wait("restart", "metadata_ready")
        assert recovered.attempts == 2 and recovered.extraction_id is not None
        checks.append("actual_task_interruption_and_replacement_recovery")
        event("replacement_recovery_passed")
        await arm("cancel")
        await wait("cancel", "running")
        async with database.sessions() as session, session.begin():
            await store.cancel(
                session, await store.owned(session, UUID(config["owner"]), identifiers["cancel"])
            )
        await asyncio.sleep(3)
        async with database.sessions() as session, session.begin():
            await store.admission_lock(session)
            cancelled = await store.owned(session, UUID(config["owner"]), identifiers["cancel"])
            assert cancelled.status == "cancelled" and cancelled.extraction_id is None
            await store.retry(session, cancelled, settings)
        retried = await wait("cancel", "metadata_ready")
        assert retried.attempts == 2
        checks.append("running_cancellation_and_explicit_retry")
        async with database.transaction() as repos:
            extractions = await repos.extractions.list(owner_id=UUID(config["owner"]))
            assert len(extractions) == 3
            for result in extractions:
                assert len(result.document["slides"]) == 3
                assert result.document["warnings"]
                assert len({page["id"] for page in result.document["slides"]}) == 3
                assert all(
                    page["width"] > 0 and page["height"] > 0 for page in result.document["slides"]
                )
            assert await repos.analyses.list(owner_id=UUID(config["owner"])) == []
        checks += ["durable_page_metadata_and_warnings", "no_false_analysis_success"]
        return {"checks": checks}
    finally:
        await database.close()
        storage.close()
        queue.close()


async def main():
    config = json.loads(os.environ["BUDDY_ACCEPTANCE_CONFIG"])
    validate(config)
    mode = sys.argv[1]
    event("started", mode=mode)
    result = await {
        "prepare": prepare,
        "cleanup": cleanup,
        "permissions": permissions,
        "exercise": exercise,
    }[mode](config)
    event("acceptance_result", mode=mode, result=result)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Provider/SQL exceptions can contain credentials. Report only stage/type.
        event("acceptance_failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None
