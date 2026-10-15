"""Alembic owns DDL; one transaction and schema-scoped advisory lock per migration run."""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection, text

from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.models import Base
from buddy_api.persistence.settings import DatabaseSettings

settings = context.config.attributes.get("database_settings") or DatabaseSettings.from_environment(
    production=os.environ.get("BUDDY_ENVIRONMENT") == "production"
)


def run(connection: Connection) -> None:
    with connection.begin():
        # Serialize migration runners before Alembic inspects the version table.
        connection.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('buddy_migrations:' || current_schema(), 0))"
            )
        )
        if connection.scalar(text("SELECT current_schema()")) != settings.schema:
            raise RuntimeError("Create the configured database schema before migrating.")
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


async def online() -> None:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=settings.connection_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    asyncio.run(online())
