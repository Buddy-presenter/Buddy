"""Internal repositories; owned reads always require an explicit account scope."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from buddy_api.extraction.contracts import Extraction
from buddy_api.persistence.errors import RecordNotFound
from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
    DeckExtraction,
    OwnedRecord,
    PresentationSession,
    ProcessingJob,
    User,
)


class OwnedRepository[T: OwnedRecord]:
    def __init__(self, session: AsyncSession, model: type[T]):
        self.session = session
        self.model = model

    async def get(self, identifier: UUID, *, owner_id: UUID) -> T:
        value = await self.session.scalar(
            select(self.model).where(
                self.model.id == identifier,
                self.model.owner_id == owner_id,
            )
        )
        if value is None:
            raise RecordNotFound("Owned record was not found.")
        return value

    async def list(self, *, owner_id: UUID, limit: int = 100) -> list[T]:
        if not 1 <= limit <= 100:
            raise ValueError("Repository limit must be from 1 to 100.")
        result = await self.session.scalars(
            select(self.model)
            .where(self.model.owner_id == owner_id)
            .order_by(self.model.created_at, self.model.id)
            .limit(limit)
        )
        return list(result)

    async def _add(self, value: T) -> T:
        self.session.add(value)
        await self.session.flush()
        return value


class Users:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, subject: str) -> User:
        user = User(subject=subject)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get(self, identifier: UUID) -> User:
        user = await self.session.get(User, identifier)
        if user is None:
            raise RecordNotFound("User was not found.")
        return user


class Decks(OwnedRepository[Deck]):
    async def create(self, *, owner_id: UUID, title: str, source_key: str | None = None) -> Deck:
        return await self._add(Deck(owner_id=owner_id, title=title, source_key=source_key))


class Jobs(OwnedRepository[ProcessingJob]):
    async def create(self, *, owner_id: UUID, deck_id: UUID) -> ProcessingJob:
        return await self._add(ProcessingJob(owner_id=owner_id, deck_id=deck_id))


class Extractions(OwnedRepository[DeckExtraction]):
    async def save(
        self,
        *,
        owner_id: UUID,
        deck_id: UUID,
        extraction: "Extraction",
    ) -> DeckExtraction:
        from buddy_api.persistence.errors import PersistenceConflict

        # Lock against concurrent owner changes/deletion; retries never overwrite a revision.
        deck = await self.session.scalar(
            select(Deck).where(Deck.id == deck_id, Deck.owner_id == owner_id).with_for_update()
        )
        if deck is None:
            raise RecordNotFound("Owned record was not found.")
        document = extraction.model_dump(mode="json")
        await self.session.execute(
            insert(DeckExtraction)
            .values(
                owner_id=owner_id,
                deck_id=deck_id,
                source_revision=extraction.source_revision,
                source_sha256=extraction.source_sha256,
                extractor_version=extraction.extractor_version,
                document=document,
            )
            .on_conflict_do_nothing(
                index_elements=["deck_id", "source_revision", "extractor_version"]
            )
        )
        value = await self.session.scalar(
            select(DeckExtraction).where(
                DeckExtraction.deck_id == deck_id,
                DeckExtraction.owner_id == owner_id,
                DeckExtraction.source_revision == extraction.source_revision,
                DeckExtraction.extractor_version == extraction.extractor_version,
            )
        )
        if value is None or value.document != document:
            raise PersistenceConflict("Source revision already has a different extraction.")
        return value


class Analyses(OwnedRepository[AnalysisVersion]):
    async def create(
        self,
        *,
        owner_id: UUID,
        deck_id: UUID,
        version: int,
        graph: dict[str, Any],
        job_id: UUID | None = None,
    ) -> AnalysisVersion:
        return await self._add(
            AnalysisVersion(
                owner_id=owner_id, deck_id=deck_id, version=version, graph=graph, job_id=job_id
            )
        )


class Sessions(OwnedRepository[PresentationSession]):
    async def create(
        self, *, owner_id: UUID, deck_id: UUID, analysis_version_id: UUID
    ) -> PresentationSession:
        return await self._add(
            PresentationSession(
                owner_id=owner_id, deck_id=deck_id, analysis_version_id=analysis_version_id
            )
        )


@dataclass
class Repositories:
    users: Users
    decks: Decks
    jobs: Jobs
    analyses: Analyses
    sessions: Sessions
    extractions: Extractions

    @classmethod
    def for_session(cls, session: AsyncSession) -> "Repositories":
        return cls(
            Users(session),
            Decks(session, Deck),
            Jobs(session, ProcessingJob),
            Analyses(session, AnalysisVersion),
            Sessions(session, PresentationSession),
            Extractions(session, DeckExtraction),
        )
