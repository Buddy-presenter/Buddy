"""Synthetic application operations inside the image under compatibility test."""

import asyncio
import inspect
import json
import runpy
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from buddy_api.persistence.database import Database
from buddy_api.persistence.errors import PersistenceConflict, RecordNotFound
from buddy_api.persistence.settings import DatabaseSettings

OWNER = UUID("10000000-0000-0000-0000-000000000001")
OTHER = UUID("10000000-0000-0000-0000-000000000002")
DECK = UUID("20000000-0000-0000-0000-000000000001")
ANALYSIS = UUID("30000000-0000-0000-0000-000000000001")
SESSION = UUID("40000000-0000-0000-0000-000000000001")
GRAPH = {"nodes": [{"id": "synthetic-slide"}], "edges": [], "marker": "rollback"}


async def run() -> None:
    bridge = Path("/opt/buddy/compatibility_bridge.py")
    if bridge.is_file():
        runpy.run_path(str(bridge))["install"]()
    db = Database(DatabaseSettings.from_environment())
    mode = sys.argv[1]
    try:
        await db.check_ready()
        if mode == "seed":
            async with db.transaction() as repos:
                await repos.analyses.session.execute(
                    text(
                        "INSERT INTO users(id,subject) VALUES "
                        "(:a,'rollback-owner'),(:b,'rollback-other')"
                    ),
                    {"a": OWNER, "b": OTHER},
                )
                await repos.analyses.session.execute(
                    text(
                        "INSERT INTO decks(id,owner_id,title) VALUES (:id,:owner,'Synthetic "
                        "rollback')"
                    ),
                    {"id": DECK, "owner": OWNER},
                )
                await repos.analyses.session.execute(
                    text(
                        "INSERT INTO analysis_versions(id,owner_id,deck_id,version,graph) "
                        "VALUES (:id,:owner,:deck,1,CAST(:graph AS jsonb))"
                    ),
                    {"id": ANALYSIS, "owner": OWNER, "deck": DECK, "graph": json.dumps(GRAPH)},
                )
                await repos.analyses.session.execute(
                    text(
                        "INSERT INTO "
                        "presentation_sessions(id,owner_id,deck_id,analysis_version_id) "
                        "VALUES (:id,:owner,:deck,:analysis)"
                    ),
                    {"id": SESSION, "owner": OWNER, "deck": DECK, "analysis": ANALYSIS},
                )
                # Exercise the old image's actual write method before expansion.
                extra = await repos.analyses.create(
                    owner_id=OWNER, deck_id=DECK, version=3, graph=GRAPH
                )
                assert (await repos.analyses.get(extra.id, owner_id=OWNER)).graph == GRAPH
        if mode == "typed":
            # Only synthetic fixture data is loaded; create/read still execute
            # the packaged image's real extraction and analysis repositories.
            fixture = runpy.run_path("/support/analysis.py")
            fixture["document"].__globals__["FIXTURE"] = Path("/fixtures/analysis-v1.json")
            async with db.transaction() as repos:
                created = await fixture["create"](repos, owner_id=OWNER, deck_id=DECK, version=4)
                assert created.graph == {}
                result = await repos.analyses.read(created.id, owner_id=OWNER)
                assert result.inputs is not None and result.publication != "legacy"
        async with db.transaction() as repos:
            record = await repos.analyses.get(ANALYSIS, owner_id=OWNER)
            assert record.graph == GRAPH
            assert any(row.id == ANALYSIS for row in await repos.analyses.list(owner_id=OWNER))
            assert (
                await repos.sessions.get(SESSION, owner_id=OWNER)
            ).analysis_version_id == ANALYSIS
            try:
                await repos.analyses.get(ANALYSIS, owner_id=OTHER)
            except RecordNotFound:
                pass
            else:
                raise AssertionError("Cross-owner analysis read succeeded")
            if mode == "write" and "graph" in inspect.signature(repos.analyses.create).parameters:
                created = await repos.analyses.create(
                    owner_id=OWNER, deck_id=DECK, version=2, graph=GRAPH
                )
                assert (await repos.analyses.get(created.id, owner_id=OWNER)).graph == GRAPH
                archived = await repos.analyses.session.scalar(
                    text("SELECT payload FROM legacy_analysis_payloads WHERE analysis_id=:id"),
                    {"id": created.id},
                )
                assert archived == GRAPH
                resumed = await repos.sessions.create(
                    owner_id=OWNER, deck_id=DECK, analysis_version_id=created.id
                )
                assert (
                    await repos.sessions.get(resumed.id, owner_id=OWNER)
                ).analysis_version_id == created.id
            # New readers must continue treating archived legacy data as legacy.
            if hasattr(repos.analyses, "read"):
                for row in await repos.analyses.list(owner_id=OWNER):
                    result = await repos.analyses.read(row.id, owner_id=OWNER)
                    if row.version == 4:
                        assert result.publication != "legacy" and result.inputs is not None
                    else:
                        assert result.publication == "legacy" and result.inputs is None
        if mode not in {"seed", "baseline"}:
            try:
                async with db.transaction() as repos:
                    await repos.analyses.session.execute(
                        text("UPDATE analysis_versions SET graph='{}'::jsonb WHERE id=:id"),
                        {"id": ANALYSIS},
                    )
            except PersistenceConflict:
                pass
            else:
                raise AssertionError("Immutable graph was changed")
        print(
            json.dumps(
                {
                    "ready": True,
                    "analysis": True,
                    "session": True,
                    "owner_isolation": True,
                    "mode": mode,
                }
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(run())
