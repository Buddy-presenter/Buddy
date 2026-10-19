"""One bounded worker process per container; durable recovery after interruption."""

import argparse
import asyncio
import logging
import os
import signal
import time

from buddy_api.persistence.database import Database
from buddy_api.persistence.settings import DatabaseSettings
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.queue import Queue
from buddy_api.processing.scratch import root, workspace
from buddy_api.processing.service import Processor
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.service import blocking
from buddy_api.uploads.storage import SourceStorage
from buddy_api.validation.config import ValidatorSettings

HEALTH = root() / "health"


async def run() -> None:
    environment = os.environ.get("BUDDY_ENVIRONMENT")
    if environment not in {"development", "test", "production"}:
        raise ValueError("Worker requires an explicit environment")
    database_settings = DatabaseSettings.from_environment(production=environment == "production")
    uploads = UploadSettings.from_environment(os.environ)
    settings = WorkerSettings.from_environment(os.environ)
    if not uploads or not settings:
        raise ValueError("Worker requires database, private storage and SQS configuration.")
    logging.info("Worker configuration validated")
    database, storage, queue = (
        Database(database_settings),
        SourceStorage(uploads),
        Queue(settings),
    )
    validator_python = os.environ.get("BUDDY_VALIDATOR_PYTHON")
    if not validator_python:
        raise ValueError("Worker requires the isolated PDF action validator runtime.")
    processor = Processor(
        database,
        storage,
        settings,
        queue,
        validator_settings=ValidatorSettings(python=validator_python, seconds=15),
    )
    await database.check_ready()
    logging.info("Worker database ready")
    await blocking(storage.check)
    logging.info("Worker private storage ready")
    await blocking(queue.check)
    logging.info("Worker queue ready")
    await processor.maintain(publish=False)
    logging.info("Worker reconciliation ready")
    # Each container owns its scratch volume. Never sweep a shared host temp directory.
    with workspace():

        async def tick() -> None:
            while True:
                await database.check_ready()
                await blocking(queue.check)
                HEALTH.write_text(str(time.time()), encoding="ascii")
                await asyncio.sleep(10)

        async def consume() -> None:
            while True:
                for message in await blocking(queue.receive):
                    await processor.run_message(message)

        tasks = [
            asyncio.create_task(tick()),
            asyncio.create_task(processor.dispatch(publish=False)),
            asyncio.create_task(consume()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            HEALTH.unlink(missing_ok=True)
            await database.close()
            storage.close()
            queue.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        try:
            age = time.time() - float(HEALTH.read_text(encoding="ascii"))
            return 0 if 0 <= age < 40 else 1
        except (OSError, ValueError):
            return 1
    logging.basicConfig(level=logging.INFO)

    # asyncio.run cancellation drains parser and persists interruption on SIGTERM.
    async def serve() -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(run())
        if os.name != "nt":
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        await task

    try:
        asyncio.run(serve())
    except asyncio.CancelledError:
        return 0
    except Exception:
        logging.error("Worker stopped; verify configuration, migrations and dependencies")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
