"""Internal owner-scoped persistence entry point for the later worker; no public route."""

import asyncio
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from buddy_api.extraction.contracts import Limits
from buddy_api.extraction.runner import extract_pptx
from buddy_api.persistence.database import Database
from buddy_api.persistence.models import DeckExtraction


async def extract_and_store(
    database: Database,
    source: Path | BinaryIO,
    *,
    owner_id: UUID,
    deck_id: UUID,
    source_revision: str,
    limits: Limits | None = None,
) -> DeckExtraction:
    # Authorize before spending extraction resources; save rechecks inside its transaction.
    async with database.transaction() as repos:
        await repos.decks.get(deck_id, owner_id=owner_id)
    extraction = await asyncio.to_thread(
        extract_pptx, source, source_revision=source_revision, limits=limits
    )
    async with database.transaction() as repos:
        return await repos.extractions.save(
            owner_id=owner_id, deck_id=deck_id, extraction=extraction
        )
