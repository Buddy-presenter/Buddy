"""Internal repositories; owned reads always require an explicit account scope."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buddy_api.persistence.errors import RecordNotFound
from buddy_api.persistence.models import (
    AnalysisVersion,
    Deck,
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

    @classmethod
    def for_session(cls, session: AsyncSession) -> "Repositories":
        return cls(
            Users(session),
            Decks(session, Deck),
            Jobs(session, ProcessingJob),
            Analyses(session, AnalysisVersion),
            Sessions(session, PresentationSession),
        )
