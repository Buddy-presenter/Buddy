"""Opt-in #128 live S3/Bedrock + local PostgreSQL/API + real Office/Qt acceptance.

Uses a synthetic owner/deck in an isolated *_test database schema. Cognito is replaced
only at the external identity boundary on a loopback-only test API. SQS delivery is
supplied directly, so no messages reach the shared staging queue. Own S3 versions and
the test schema are removed at the end; evidence contains only synthetic slides.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4

import boto3  # type: ignore[import-untyped]
import uvicorn
from backend.tests.support.database import migrate
from backend.tests.support.powerpoint import source
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema

from buddy_api.analysis.store import Analyses
from buddy_api.app import create_app
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.models import ActionCheck, Deck, DeckUpload, ProcessingJob
from buddy_api.persistence.settings import DatabaseSettings
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message
from buddy_api.processing.service import Processor
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.storage import SourceStorage
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.service import ValidationService
from buddy_desktop.auth import Tokens
from buddy_desktop.config import AppConfig
from buddy_desktop.powerpoint import PowerPointAutomation
from buddy_desktop.presentation_service import PresentationService
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow


class LocalDelivery:
    def acknowledge(self, _receipt):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-aws", action="store_true", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="eu-central-1")
    args = parser.parse_args()
    if sys.platform != "win32" or PowerPointAutomation._powerpoint_process_running():
        parser.error("Use a dedicated Windows/Office session with no existing PowerPoint process.")
    url = os.environ["BUDDY_TEST_DATABASE_URL"]
    admin = DatabaseSettings(url)
    if not admin.connection_url().database.endswith("_test"):
        parser.error("Use a disposable *_test PostgreSQL database.")
    schema = "buddy_issue128_" + uuid4().hex
    database_settings = DatabaseSettings(url, schema)
    owner = uuid4()
    folder = Path("build/issue128/live-workflow").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    content, _ = source()
    local_source = folder / "Quarterly-results.pptx"
    local_source.write_bytes(content)
    s3 = boto3.client("s3", region_name=args.region)
    encryption = s3.get_bucket_encryption(Bucket=args.bucket)
    kms = encryption["ServerSideEncryptionConfiguration"]["Rules"][0][
        "ApplyServerSideEncryptionByDefault"
    ]["KMSMasterKeyID"]
    uploads = UploadSettings(bucket=args.bucket, region=args.region, kms_key=kms)
    storage = SourceStorage(uploads)
    worker_settings = WorkerSettings(
        queue_url=f"https://sqs.{args.region}.amazonaws.com/111122223333/unused-local-delivery",
        region=args.region,
    )
    keys: set[str] = set()
    server = thread = window = None
    created_schema = False
    qt = QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    apply_theme(qt)
    report = {
        "scope": "live staging S3/Bedrock; isolated local API/database/Office/Qt",
        "synthetic": True,
    }

    async def prepare():
        nonlocal owner, created_schema
        engine = create_engine(admin)
        try:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            created_schema = True
        finally:
            await engine.dispose()
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            async with db.transaction() as repos:
                owner = (await repos.users.create("synthetic-issue128-" + uuid4().hex)).id
        finally:
            await db.close()

    def wait(predicate, seconds=90):
        deadline = time.monotonic() + seconds
        while not predicate():
            qt.processEvents()
            if time.monotonic() > deadline:
                raise TimeoutError("Synthetic workflow step exceeded its deadline.")
            time.sleep(0.01)
        qt.processEvents()

    async def process(deck_id):
        db = Database(database_settings)
        try:
            async with db.sessions() as session:
                job = await session.scalar(
                    select(ProcessingJob).where(ProcessingJob.deck_id == deck_id)
                )
            processor = Processor(
                db,
                storage,
                worker_settings,
                LocalDelivery(),
                validator_settings=ValidatorSettings(python=sys.executable, seconds=15),
            )
            await processor.run_message(
                {
                    "Body": Message(job_id=job.id, generation=job.generation).model_dump_json(),
                    "ReceiptHandle": "local-test-delivery",
                }
            )
            async with db.sessions() as session:
                current = await session.get(ProcessingJob, job.id)
                record = await Analyses(session).latest_for_deck(deck_id, owner_id=owner)
                result = await Analyses(session).read(record.id, owner_id=owner)
                report.update(
                    job_status=current.status,
                    slides=len(result.slides),
                    actions=sum(len(s.actions) for s in result.slides),
                    usable=sum(a.usable for s in result.slides for a in s.actions),
                )
                (folder / "saved-analysis.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
                if current.status != "analysis_ready" or not all(
                    a.usable for s in result.slides for a in s.actions
                ):
                    raise AssertionError(
                        "Live generated actions were not fully validated. "
                        "Inspect synthetic evidence."
                    )
        finally:
            await db.close()

    async def cleanup():
        if not created_schema:
            return
        db = Database(database_settings)
        try:
            async with db.sessions() as session:
                for row in (
                    await session.scalars(select(DeckUpload).where(DeckUpload.owner_id == owner))
                ).all():
                    keys.add(row.storage_key)
                for row in (
                    await session.scalars(select(Deck).where(Deck.owner_id == owner))
                ).all():
                    if row.source_key:
                        keys.add(row.source_key)
                for row in (
                    await session.scalars(select(ActionCheck).where(ActionCheck.owner_id == owner))
                ).all():
                    for kind in ("report", "before_image", "after_image"):
                        keys.add(ValidationService.key(row, kind))
            (folder / "resource-receipt.json").write_text(
                json.dumps(
                    {"bucket": args.bucket, "owner": str(owner), "keys": sorted(keys)}, indent=2
                )
            )
            for key in keys:
                if not key.startswith(f"sources/{owner}/"):
                    raise AssertionError("Cleanup escaped the synthetic owner's exact prefix.")
                storage.remove(key)
            report["s3_keys_cleaned"] = len(keys)
        finally:
            await db.close()
        engine = create_engine(admin)
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
            report["test_schema_removed"] = True
        finally:
            await engine.dispose()

    try:
        asyncio.run(prepare())
        api = create_app(
            Settings(Environment.TEST, ("127.0.0.1",), database=database_settings, uploads=uploads)
        )
        api.dependency_overrides[authenticated] = lambda: Principal(owner, "synthetic@example.test")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(api, log_level="error", access_log=False))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        wait(lambda: server.started)
        config = AppConfig(f"http://127.0.0.1:{port}", 10)
        saved = PresentationService(config, "synthetic-loopback-identity").upload(
            local_source, uuid4()
        )
        deck_id = UUID(saved.deck_id)
        print(json.dumps({"stage": "uploaded_to_staging_s3", "synthetic": True}), flush=True)
        asyncio.run(process(deck_id))
        print(json.dumps({"stage": "live_provider_and_office_validated", **report}), flush=True)
        window = MainWindow(config, QSettings(str(folder / "ui.ini"), QSettings.Format.IniFormat))
        window.resize(1360, 1000)
        window.show()
        window.account.session = SimpleNamespace(
            tokens=Tokens(
                "synthetic-loopback-identity", "synthetic-refresh", time.monotonic() + 3600
            ),
            cancelled=Event(),
        )
        window.account.session_changed.emit()
        window.navigation.setCurrentRow(2)
        review = window.review
        review.set_deck(deck_id, "Quarterly results.pptx", "review_ready", "pptx")
        wait(lambda: not review.runner.busy)
        if review.analysis is None or review.pdf_content != content:
            raise AssertionError(
                "Actual desktop could not read the saved PowerPoint analysis/source."
            )
        if not review.open_powerpoint.isEnabled():
            raise AssertionError("The saved PowerPoint opening control was disabled.")
        review.open_powerpoint.click()
        wait(lambda: not review.runner.busy)
        if review.image.isNull():
            raise AssertionError(
                "Actual desktop could not open its PowerPoint source: " + review.notice.text()
            )
        executions = []
        for index in range(len(review.analysis.slides)):
            review.show_page(index)
            wait(lambda: not review.runner.busy)
            for row in range(review.action_table.rowCount()):
                review.action_table.selectRow(row)
                qt.processEvents()
                if not review.run_powerpoint.isEnabled():
                    raise AssertionError("A validated action was unavailable in the actual UI.")
                kind = review.action_rows[row].action_type
                review.run_powerpoint.click()
                wait(lambda: not review.runner.busy)
                if "action completed" not in review.notice.text():
                    raise AssertionError("The actual UI failed to execute its saved action.")
                assert window.grab().save(
                    str(folder / f"ui-slide-{index + 1}-{row + 1}-{kind}.png")
                )
                executions.append(
                    {"slide": index + 1, "type": kind, "state": "executed_in_real_powerpoint"}
                )
        report["ui_executions"] = executions
        window.account.session.tokens = None
        window.account.session_changed.emit()
        wait(lambda: not review.cleanup_runner.busy)
        if review.analysis is not None or review.pdf_content is not None:
            raise AssertionError("Sign-out retained private review data.")
        report["signout_cleanup"] = True
        print(
            json.dumps(
                {"stage": "ui_verified", "actions": len(executions), "signout_cleanup": True}
            ),
            flush=True,
        )
    finally:
        if window is not None:
            window.close()
            wait(lambda: not window.review.runner.busy and not window.review.cleanup_runner.busy)
            window.review.runner.wait_for_shutdown()
            window.review.cleanup_runner.wait_for_shutdown()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=15)
        try:
            asyncio.run(cleanup())
        finally:
            storage.close()
            s3.close()
            (folder / "acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
