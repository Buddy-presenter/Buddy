"""Transactional units of work and honest database readiness, without automatic DDL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.errors import PersistenceConflict, PersistenceUnavailable
from buddy_api.persistence.migration_config import migration_heads
from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
    PresentationSession,
    ProcessingJob,
    RenderedAsset,
    User,
)
from buddy_api.persistence.repositories import Repositories
from buddy_api.persistence.settings import DatabaseSettings


class Database:
    def __init__(self, settings: DatabaseSettings):
        self.engine = create_engine(settings)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.expected_heads = migration_heads(settings)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Repositories]:
        try:
            async with self.sessions() as session, session.begin():
                yield Repositories.for_session(session)
        except (IntegrityError, DataError):
            raise PersistenceConflict(
                "The operation conflicts with stored data or ownership constraints."
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise PersistenceUnavailable(
                "Database operation failed; verify connectivity and migrations."
            ) from None

    async def check_ready(self) -> None:
        try:
            async with self.engine.connect() as connection:
                revisions = set(
                    (
                        await connection.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalars()
                )
                if revisions != self.expected_heads:
                    raise PersistenceUnavailable("Database migrations are not current.")
                # Read no private rows, but detect missing tables as well as missing revisions.
                for identifier in (
                    User.id,
                    Deck.id,
                    ProcessingJob.id,
                    AnalysisVersion.id,
                    PresentationSession.id,
                    RenderedAsset.id,
                ):
                    await connection.execute(select(identifier).limit(0))
        except (SQLAlchemyError, OSError, TimeoutError):
            raise PersistenceUnavailable(
                "Database readiness failed; verify connectivity and migrations."
            ) from None

    async def close(self) -> None:
        await self.engine.dispose()


class DatabaseProbe:
    name = "database"

    def __init__(self, database: Database | None):
        self.database = database

    async def check(self) -> None:
        if self.database is None:
            raise PersistenceUnavailable("Database is not configured.")
        await self.database.check_ready()
