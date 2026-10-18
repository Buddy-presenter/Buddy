"""Real database checks against the previous source contract and bridge readiness."""

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text

from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.migration_config import migration_config

ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_REVISION = "dc1bb7743f9fa95e5fd75fe7a488ca5262c06b75"
pytestmark = pytest.mark.postgres


@pytest.fixture(scope="session")
def previous_source(tmp_path_factory):
    folder = tmp_path_factory.mktemp("previous-app-source")
    archive = folder / "previous.zip"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            "--output=" + str(archive),
            PREVIOUS_REVISION,
            "backend",
            "shared",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(folder)
    return folder


def old_app(previous_source, settings, mode, *, bridge=False):
    code = """
import runpy, sys
from pathlib import Path
import buddy_api
assert Path(buddy_api.__file__).resolve().is_relative_to(Path(sys.argv[1]))
root = Path(sys.argv[2])
if sys.argv[3] == 'bridge':
    runpy.run_path(str(root/'backend/scripts/release/compatibility_bridge.py'))['install']()
sys.argv = ['rollback_probe', sys.argv[4]]
runpy.run_path(str(root/'backend/scripts/release/rollback_probe.py'), run_name='__main__')
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(previous_source),
            str(ROOT),
            "bridge" if bridge else "old",
            mode,
        ],
        cwd=previous_source,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(previous_source / "backend/src"), str(previous_source / "shared/src")]
            ),
            "BUDDY_DATABASE_URL": settings.url,
            "BUDDY_DATABASE_SCHEMA": settings.schema,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


async def upgrade(settings, revision):
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(migration_config(settings, sync), revision)
            )
    finally:
        await engine.dispose()


def test_bridge_preserves_old_reads_writes_sessions_and_new_readiness(
    database_settings, previous_source
):
    asyncio.run(upgrade(database_settings, "0003_extractions"))
    original = old_app(previous_source, database_settings, "seed")
    assert original.returncode == 0, original.stderr
    baseline = old_app(previous_source, database_settings, "baseline", bridge=True)
    assert baseline.returncode == 0, baseline.stderr
    asyncio.run(upgrade(database_settings, "head"))
    # The original immutable image remains an invalid rollback target; the
    # compatibility release must become the recorded previous app first.
    incompatible = old_app(previous_source, database_settings, "read")
    assert incompatible.returncode != 0
    assert "Database migrations are not current" in incompatible.stderr
    compatible = old_app(previous_source, database_settings, "write", bridge=True)
    assert compatible.returncode == 0, compatible.stderr
    assert json.loads(compatible.stdout)["owner_isolation"] is True

    async def verify():
        db = Database(database_settings)
        try:
            await db.check_ready()
            async with db.transaction() as repos:
                from backend.scripts.release.rollback_probe import OWNER

                records = await repos.analyses.list(owner_id=OWNER)
                assert {record.version for record in records} == {1, 2, 3}
                for record in records:
                    read = await repos.analyses.read(record.id, owner_id=OWNER)
                    assert read.publication == "legacy" and read.inputs is None
                    archive = await repos.analyses.session.scalar(
                        text("SELECT payload FROM legacy_analysis_payloads WHERE analysis_id=:id"),
                        {"id": record.id},
                    )
                    assert archive == record.graph
        finally:
            await db.close()

    asyncio.run(verify())


def test_bridge_rejects_an_untested_intermediate_schema(database_settings, previous_source):
    asyncio.run(upgrade(database_settings, "0004_uploads"))
    result = old_app(previous_source, database_settings, "baseline", bridge=True)
    assert result.returncode != 0
    assert "outside the tested compatibility window" in result.stderr
