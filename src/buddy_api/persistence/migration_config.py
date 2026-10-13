"""Locate immutable packaged migrations, independent of the working directory."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from buddy_api.persistence.settings import DatabaseSettings


def migration_config(settings: DatabaseSettings, connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    config.attributes["database_settings"] = settings
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def migration_heads(settings: DatabaseSettings) -> set[str]:
    return set(ScriptDirectory.from_config(migration_config(settings)).get_heads())
