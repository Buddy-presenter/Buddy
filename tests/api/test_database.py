"""Real PostgreSQL checks; every test owns an isolated, disposable schema."""

import asyncio
import os
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.schema import CreateSchema, DropSchema

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.errors import PersistenceConflict, PersistenceUnavailable, RecordNotFound
from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.settings import DatabaseSettings

pytestmark = pytest.mark.postgres


def test_cognito_account_is_persisted_across_api_restart(database_settings, monkeypatch):
    from buddy_api.auth import CognitoVerifier
    from buddy_identity.config import IdentityConfig

    config = IdentityConfig(
        "eu-central-1",
        "eu-central-1_Example",
        "client123",
        "https://buddy.auth.eu-central-1.amazoncognito.com",
    )
    subject = config.issuer + "#" + str(uuid4())
    monkeypatch.setattr(CognitoVerifier, "verify", lambda self, token: subject)
    asyncio.run(migrate(database_settings))
    settings = Settings(
        Environment.TEST, ("testserver",), database=database_settings, identity=config
    )
    identifiers = []
    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer synthetic"})
            assert response.status_code == 200
            identifiers.append(response.json()["data"]["id"])
            assert client.get("/api/v1/auth/me").status_code == 401
    assert identifiers[0] == identifiers[1]


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


async def seed(database, subject="synthetic-owner"):
    async with database.transaction() as repos:
        user = await repos.users.create(subject)
        deck = await repos.decks.create(
            owner_id=user.id, title="Synthetic deck", source_key="private/synthetic.pptx"
        )
        job = await repos.jobs.create(owner_id=user.id, deck_id=deck.id)
        analysis = await repos.analyses.create(
            owner_id=user.id,
            deck_id=deck.id,
            job_id=job.id,
            version=1,
            graph={"slides": [{"id": "slide-1"}]},
        )
        session = await repos.sessions.create(
            owner_id=user.id, deck_id=deck.id, analysis_version_id=analysis.id
        )
        return user.id, deck.id, job.id, analysis.id, session.id


def test_empty_migrations_are_repeatable_and_match_models(database_settings):
    async def scenario():
        await migrate(database_settings)
        await migrate(database_settings)
        await migrate(database_settings, "check")
        db = Database(database_settings)
        try:
            await db.check_ready()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_data_survives_api_restarts_with_real_readiness(database_settings):
    asyncio.run(migrate(database_settings))
    settings = Settings(Environment.TEST, ("testserver",), database=database_settings)
    first = create_app(settings)
    with TestClient(first) as client:
        assert client.get("/api/v1/ready").json()["data"]["dependencies"] == [
            {"name": "database", "status": "ready"}
        ]
        owner, deck, job, analysis, session = client.portal.call(seed, first.state.database)
    second = create_app(settings)

    async def read():
        async with second.state.database.transaction() as repos:
            assert (await repos.users.get(owner)).subject == "synthetic-owner"
            assert (
                await repos.decks.get(deck, owner_id=owner)
            ).source_key == "private/synthetic.pptx"
            assert (await repos.jobs.get(job, owner_id=owner)).status == "pending"
            assert (await repos.analyses.get(analysis, owner_id=owner)).graph == {
                "slides": [{"id": "slide-1"}]
            }
            assert (
                await repos.sessions.get(session, owner_id=owner)
            ).analysis_version_id == analysis

    with TestClient(second) as client:
        assert client.get("/api/v1/ready").status_code == 200
        client.portal.call(read)


def test_repository_ownership_and_database_cross_owner_constraints(database_settings):
    async def scenario():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            owner, deck, job, analysis, session = await seed(db)
            async with db.transaction() as repos:
                other = await repos.users.create("other-synthetic-owner")
            for repository, identifier in [
                ("decks", deck),
                ("jobs", job),
                ("analyses", analysis),
                ("sessions", session),
            ]:
                with pytest.raises(RecordNotFound):
                    async with db.transaction() as repos:
                        await getattr(repos, repository).get(identifier, owner_id=other.id)
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.jobs.create(owner_id=other.id, deck_id=deck)
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.analyses.create(
                        owner_id=other.id, deck_id=deck, version=2, graph={}
                    )
            async with db.transaction() as repos:
                other_deck = await repos.decks.create(owner_id=owner, title="Other deck")
                assert await repos.decks.list(owner_id=other.id) == []
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.sessions.create(
                        owner_id=owner, deck_id=other_deck.id, analysis_version_id=analysis
                    )
        finally:
            await db.close()

    asyncio.run(scenario())


def test_transaction_rolls_back_and_next_unit_of_work_recovers(database_settings):
    async def scenario():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            owner, deck, *_ = await seed(db)
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.decks.create(owner_id=owner, title="must roll back")
                    await repos.users.create("synthetic-owner")
            async with db.transaction() as repos:
                assert [d.id for d in await repos.decks.list(owner_id=owner)] == [deck]
                with pytest.raises(ValueError):
                    await repos.decks.list(owner_id=owner, limit=101)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_parameterized_text_and_version_uniqueness(database_settings):
    async def scenario():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            owner, deck, *_ = await seed(db)
            title = "'); DROP TABLE users; --"
            async with db.transaction() as repos:
                literal = await repos.decks.create(owner_id=owner, title=title)
                assert (await repos.decks.get(literal.id, owner_id=owner)).title == title
            with pytest.raises(PersistenceConflict):
                async with db.transaction() as repos:
                    await repos.analyses.create(owner_id=owner, deck_id=deck, version=1, graph={})
            await db.check_ready()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_unmigrated_and_outdated_database_is_not_ready(database_settings):
    settings = Settings(Environment.TEST, ("testserver",), database=database_settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert "buddy-local" not in response.text
        assert client.get("/health").status_code == 200
    asyncio.run(migrate(database_settings))

    async def corrupt():
        engine = create_engine(database_settings)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num='unknown_revision'")
                )
        finally:
            await engine.dispose()

    asyncio.run(corrupt())
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/ready").status_code == 503


def test_connection_failure_is_redacted_and_liveness_remains_available():
    settings = DatabaseSettings(
        "postgresql://buddy:private-secret@127.0.0.1:1/buddy_test?sslmode=disable", timeout=0.2
    )
    app = create_app(Settings(Environment.TEST, ("testserver",), database=settings))
    with TestClient(app) as client:
        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        assert "private-secret" not in response.text
        assert client.get("/health").status_code == 200

    async def write():
        db = Database(settings)
        try:
            with pytest.raises(PersistenceUnavailable) as exc:
                async with db.transaction() as repos:
                    await repos.users.create("private-subject")
            assert "private" not in str(exc.value)
        finally:
            await db.close()

    asyncio.run(write())


def test_migration_failure_is_safe_and_keeps_existing_data(database_settings):
    async def prepare():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            await seed(db)
            async with db.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num='unknown_revision'")
                )
        finally:
            await db.close()

    asyncio.run(prepare())
    env = {
        **os.environ,
        "BUDDY_DATABASE_URL": database_settings.url,
        "BUDDY_DATABASE_SCHEMA": database_settings.schema,
        "BUDDY_ENVIRONMENT": "test",
    }
    result = subprocess.run(
        [sys.executable, "-m", "buddy_api.persistence.migrate", "upgrade"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "migration operation failed" in result.stderr
    assert "buddy-local-test-only" not in result.stderr

    async def read():
        db = Database(database_settings)
        try:
            async with db.engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT count(*) FROM users"))
                ).scalar_one() == 1
        finally:
            await db.close()

    asyncio.run(read())


def test_concurrent_migration_processes_serialize(database_settings):
    env = {
        **os.environ,
        "BUDDY_DATABASE_URL": database_settings.url,
        "BUDDY_DATABASE_SCHEMA": database_settings.schema,
        "BUDDY_ENVIRONMENT": "test",
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "buddy_api.persistence.migrate", "upgrade"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    asyncio.run(migrate(database_settings, "check"))


def test_failed_ddl_migration_rolls_back_atomically(database_settings, tmp_path):
    previous_head = ScriptDirectory.from_config(
        migration_config(database_settings)
    ).get_current_head()
    folder = tmp_path / "migrations"
    shutil.copytree(migration_config(database_settings).get_main_option("script_location"), folder)
    (folder / "versions/0002_failure.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        f'revision="synthetic_failure"\ndown_revision="{previous_head}"\n'
        "def upgrade():\n"
        '    op.create_table("migration_probe", sa.Column("id",sa.Integer()))\n'
        '    raise RuntimeError("synthetic migration failure")\n'
        "def downgrade():\n    pass\n",
        encoding="utf-8",
    )

    async def scenario():
        await migrate(database_settings)
        db = Database(database_settings)
        try:
            await seed(db)
            async with db.engine.connect() as connection:

                def fail(sync):
                    config = migration_config(database_settings, sync)
                    config.set_main_option("script_location", str(folder))
                    command.upgrade(config, "head")

                with pytest.raises(RuntimeError, match="synthetic migration failure"):
                    await connection.run_sync(fail)
            async with db.engine.connect() as connection:
                assert (
                    await connection.execute(text("SELECT to_regclass('migration_probe')"))
                ).scalar_one() is None
                assert (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one() == previous_head
                assert (
                    await connection.execute(text("SELECT count(*) FROM users"))
                ).scalar_one() == 1
            await db.check_ready()
        finally:
            await db.close()

    asyncio.run(scenario())
