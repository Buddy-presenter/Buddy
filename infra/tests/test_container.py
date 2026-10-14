"""Container boundaries and a real PostgreSQL role/migration integration in isolated CI."""

import asyncio
import importlib.util
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "infra/container" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/v4/id",
        "http://169.254.170.2.evil.example/v4/id",
        "http://user@169.254.170.2/v4/id",
        "http://169.254.170.2/v4/id?secret=value",
    ],
)
def test_reject_untrusted_metadata(uri):
    with pytest.raises(ValueError):
        load("container_entrypoint").metadata_hosts(uri)


def test_bootstrap_errors_do_not_print_credentials(monkeypatch, capsys):
    bootstrap = load("bootstrap_database")

    async def failure():
        raise ValueError("password=must-not-appear")

    monkeypatch.setattr(bootstrap, "provision", failure)
    assert bootstrap.main() == 1
    assert "must-not-appear" not in capsys.readouterr().err


@pytest.mark.skipif(
    os.environ.get("BUDDY_BOOTSTRAP_TEST") != "true", reason="Isolated CI PostgreSQL required"
)
def test_real_roles_migrations_retry_and_least_privilege(monkeypatch):
    bootstrap = load("bootstrap_database")

    class Missing(Exception):
        pass

    class Secrets:
        class exceptions:
            ResourceNotFoundException = Missing

        def __init__(self):
            self.values = {"admin": {"username": "buddyadmin", "password": "buddy-ci-test-only"}}

        def get_secret_value(self, SecretId):
            if SecretId not in self.values:
                raise Missing()
            return {"SecretString": json.dumps(self.values[SecretId])}

        def put_secret_value(self, SecretId, SecretString):
            self.values[SecretId] = json.loads(SecretString)

    secrets = Secrets()
    monkeypatch.setattr(bootstrap.boto3, "client", lambda *args, **kwargs: secrets)
    connect = asyncpg.connect

    async def local_connect(**kwargs):
        assert isinstance(kwargs["ssl"], ssl.SSLContext)
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        # Only the disposable CI PostgreSQL service lacks TLS; production forces verification.
        return await connect(**{**kwargs, "ssl": False})

    monkeypatch.setattr(bootstrap.asyncpg, "connect", local_connect)
    monkeypatch.setenv("BUDDY_AWS_REGION", "eu-central-1")
    monkeypatch.setenv("BUDDY_DB_ADMIN_SECRET", "admin")
    monkeypatch.setenv("BUDDY_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("BUDDY_DB_SECRET_ARNS", json.dumps({key: key for key in bootstrap.ROLES}))
    assert bootstrap.main() == 0
    first = dict(secrets.values)
    assert bootstrap.main() == 0
    assert secrets.values == first
    migration_url = secrets.values["migration"]["url"].replace("verify-full", "disable")
    environment = {**os.environ, "BUDDY_ENVIRONMENT": "test", "BUDDY_DATABASE_URL": migration_url}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(ROOT / "infra/container/migrate_database.py")],
            env=environment,
            capture_output=True,
        )
        assert result.returncode == 0, "Migration or metadata restriction failed"

    async def check():
        for service in ("api", "worker"):
            secret = secrets.values[service]
            connection = await connect(
                host="127.0.0.1",
                user=secret["username"],
                password=secret["password"],
                database="buddy",
                ssl=False,
            )
            try:
                await connection.execute("SELECT version_num FROM alembic_version")
                await connection.execute(
                    "INSERT INTO users (id, subject) VALUES ($1, $2)",
                    __import__("uuid").uuid4(),
                    service,
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute("CREATE TABLE unauthorized (id integer)")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute("DELETE FROM alembic_version")
            finally:
                await connection.close()

    asyncio.run(check())
