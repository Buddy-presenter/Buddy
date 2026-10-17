"""Internal owner-scoped persistence entry point for the later worker; no public route."""

import asyncio
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from buddy_api.extraction.contracts import Limits
from buddy_api.extraction.runner import extract_source
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
    source_format: str | None = None,
) -> DeckExtraction:
    # Authorize before spending extraction resources; save rechecks inside its transaction.
    async with database.transaction() as repos:
        deck = await repos.decks.get(deck_id, owner_id=owner_id)
        if source_format is not None and source_format != deck.source_format:
            raise ValueError("Source format does not match the saved deck.")
    extraction = await asyncio.to_thread(
        extract_source,
        source,
        source_revision=source_revision,
        limits=limits,
        source_format=deck.source_format,
    )
    async with database.transaction() as repos:
        return await repos.extractions.save(
            owner_id=owner_id, deck_id=deck_id, extraction=extraction
        )
