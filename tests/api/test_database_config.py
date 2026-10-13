import pytest

from buddy_api.config import ConfigurationError, Environment, Settings
from buddy_api.persistence.migrate import main
from buddy_api.persistence.settings import DatabaseConfigurationError, DatabaseSettings


@pytest.mark.parametrize(
    "url",
    [
        "invalid-secret",
        "sqlite:///secret.db",
        "postgresql://buddy:secret@/database",
        "postgresql://buddy:secret@localhost",
        "postgresql://buddy:secret@localhost:0/db",
        "postgresql://buddy:secret@localhost/db?sslmode=invalid",
        "postgresql://buddy:secret@localhost/db?untrusted=secret",
    ],
)
def test_invalid_database_urls_are_redacted(url):
    with pytest.raises(DatabaseConfigurationError) as exc:
        DatabaseSettings(url)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "schema,timeout",
    [("public;drop", 3), ("", 3), ("public", 0), ("public", float("nan")), ("public", 31)],
)
def test_invalid_database_boundaries(schema, timeout):
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings("postgresql://buddy:secret@localhost/db", schema, timeout)


def test_production_requires_verified_tls_and_repr_hides_credentials():
    url = "postgresql://buddy:secret@localhost/db"
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings(url, production=True)
    database = DatabaseSettings(url + "?sslmode=verify-full", production=True)
    assert "secret" not in repr(database)
    assert "secret" not in repr(Settings(Environment.PRODUCTION, database=database))
    with pytest.raises(ConfigurationError):
        Settings(Environment.PRODUCTION)


def test_remote_database_requires_verified_tls():
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings("postgresql://buddy:secret@db.example.com/db?sslmode=disable")
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings("postgresql://buddy:secret@db.example.com/db")
    assert (
        DatabaseSettings("postgresql://buddy:secret@db.example.com/db?sslmode=verify-full").ssl_mode
        == "verify-full"
    )


def test_environment_loads_database_and_rejects_secret_errors():
    settings = Settings.from_environment(
        {
            "BUDDY_ENVIRONMENT": "development",
            "BUDDY_DATABASE_URL": "postgresql://buddy:secret@localhost/db",
        }
    )
    assert settings.database is not None
    assert settings.database.connection_url().drivername == "postgresql+asyncpg"
    with pytest.raises(ConfigurationError) as exc:
        Settings.from_environment(
            {"BUDDY_ENVIRONMENT": "development", "BUDDY_DATABASE_URL": "secret"}
        )
    assert "secret" not in str(exc.value)


def test_migration_cli_missing_configuration_is_safe(monkeypatch, capsys):
    monkeypatch.delenv("BUDDY_DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["buddy-db", "upgrade"])
    assert main() == 2
    assert "BUDDY_DATABASE_URL" in capsys.readouterr().err
