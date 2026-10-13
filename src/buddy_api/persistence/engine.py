"""A bounded, asynchronous PostgreSQL connection pool shared by API and workers."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from buddy_api.persistence.settings import DatabaseSettings


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.connection_url(),
        pool_size=5,
        max_overflow=0,
        pool_timeout=settings.timeout,
        pool_pre_ping=True,
        hide_parameters=True,
        echo=False,
        connect_args={
            "timeout": settings.timeout,
            "command_timeout": settings.timeout,
            "ssl": settings.ssl_mode,
            "server_settings": {
                "search_path": settings.schema,
                "statement_timeout": str(int(settings.timeout * 1000)),
                "lock_timeout": str(int(settings.timeout * 1000)),
            },
        },
    )
