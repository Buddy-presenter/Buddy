"""API lifespan clients and disposable PostgreSQL fixtures; no Qt imports."""

import asyncio
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from support.database import database_settings as database_settings
from support.database import migrate

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings


@pytest.fixture
def api_client_factory():
    """Own every application's lifespan and pool, including assertion-failure cleanup."""
    with ExitStack() as stack:

        def make(settings=None):
            config = settings or Settings(Environment.TEST, ("testserver",))
            if config.environment is not Environment.TEST:
                raise ValueError("API fixtures only accept test configuration.")
            return stack.enter_context(TestClient(create_app(config)))

        yield make


@pytest.fixture
def api_client(api_client_factory):
    return api_client_factory()


@pytest.fixture
def migrated_database(database_settings):
    asyncio.run(migrate(database_settings))
    return database_settings
