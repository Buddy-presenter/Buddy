"""Real PostgreSQL isolation and migrations shared by API and future worker tests."""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy.schema import CreateSchema, DropSchema

from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.settings import DatabaseSettings


@pytest.fixture
def database_settings():
    url = os.environ.get("BUDDY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BUDDY_TEST_DATABASE_URL to a disposable PostgreSQL database ending _test.")
    admin = DatabaseSettings(url)
    if not admin.connection_url().database.endswith("_test"):
        pytest.fail("Database integration tests require a database ending _test.")
    schema = "buddy_test_" + uuid4().hex

    async def manage(create):
        engine = create_engine(admin)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    CreateSchema(schema) if create else DropSchema(schema, cascade=True)
                )
        finally:
            await engine.dispose()

    asyncio.run(manage(True))
    try:
        yield DatabaseSettings(url, schema)
    finally:
        asyncio.run(manage(False))


async def migrate(settings, operation="upgrade"):
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:

            def apply(sync):
                config = migration_config(settings, sync)
                if operation == "upgrade":
                    command.upgrade(config, "head")
                else:
                    command.check(config)

            await connection.run_sync(apply)
    finally:
        await engine.dispose()
