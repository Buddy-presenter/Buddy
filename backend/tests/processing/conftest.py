import asyncio

import pytest
from backend.tests.support.database import database_settings as database_settings
from backend.tests.support.database import migrate


@pytest.fixture
def migrated_database(database_settings):
    asyncio.run(migrate(database_settings))
    return database_settings
