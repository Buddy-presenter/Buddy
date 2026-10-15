"""Locate immutable packaged migrations, independent of the working directory."""

from importlib.resources import files
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from buddy_api.persistence.settings import DatabaseSettings


def migration_config(settings: DatabaseSettings, connection: Connection | None = None) -> Config:
    config = Config()
    packaged = files("buddy_api").joinpath("persistence", "migrations")
    source = Path(__file__).resolve().parents[3] / "migrations"
    config.set_main_option("script_location", str(packaged if packaged.is_dir() else source))
    config.attributes["database_settings"] = settings
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def migration_heads(settings: DatabaseSettings) -> set[str]:
    return set(ScriptDirectory.from_config(migration_config(settings)).get_heads())
