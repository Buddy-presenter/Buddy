"""Durable reservations, bounded original-byte transfer, and restart reconciliation."""

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect, Request

from buddy_api.persistence.database import Database
from buddy_api.persistence.models import ActionCheck, Deck, DeckUpload
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import (
    SOURCE_TYPES,
    UploadData,
    UploadedDeck,
    UploadError,
    UploadMetadata,
)
from buddy_api.uploads.storage import SourceStorage
from buddy_api.uploads.validation import validate_source

logger = logging.getLogger(__name__)
ACTIVE = ("pending", "receiving", "cleanup")
RETRYABLE = {
    "upload_interrupted",
    "upload_timeout",
    "upload_expired",
    "storage_unavailable",
    "service_unavailable",
    "source_mismatch",
}


async def blocking[T](call: Callable[[], T]) -> T:
    """Drain local parser/S3 work before cancellation can trigger byte cleanup."""
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


class UploadService:
    def __init__(self, database: Database, settings: UploadSettings, storage: SourceStorage):
        self.database = database
        self.settings = settings
        self.storage = storage
        # Share one bounded resource slot with PDF reads and the isolated Qt validator.
        self.parser_slot = asyncio.Semaphore(1)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self.database.sessions() as session, session.begin():
                yield session
        except (SQLAlchemyError, OSError, TimeoutError):
            raise UploadError(
                503, "service_unavailable", "Upload records are temporarily unavailable.", 15
            ) from None

    @staticmethod
    async def admission_lock(session: AsyncSession) -> None:
        # Across all API replicas, serialize reservation/quota transfers into Deck.
        await session.execute(text("SELECT pg_advisory_xact_lock(704007)"))

    @staticmethod
    async def owned(
        session: AsyncSession, owner: UUID, identifier: UUID, *, lock: bool = False
    ) -> DeckUpload:
        query = select(DeckUpload).where(DeckUpload.id == identifier)
        if lock:
            query = query.with_for_update()
        record = await session.scalar(query)
        if record is None:
            raise UploadError(404, "not_found", "Upload not found.")
        if record.owner_id != owner:
            raise UploadError(403, "forbidden", "Access denied.")
        return record

    async def describe(self, session: AsyncSession, record: DeckUpload) -> UploadData:
        deck_data = None
        if record.status == "complete":
            deck = await session.get(Deck, record.deck_id)
            if deck is None or deck.owner_id != record.owner_id:
                raise UploadError(
                    503, "service_unavailable", "Upload records are temporarily unavailable."
                )
            deck_data = UploadedDeck.model_validate(
                {
                    "id": deck.id,
                    "title": deck.title,
                    "source_revision": deck.source_revision,
                    "sha256": deck.source_sha256,
                    "size_bytes": deck.source_size_bytes,
                    "slide_count": deck.source_slide_count,
                    "source_format": deck.source_format,
                }
            )
        return UploadData.model_validate(
            {
                "id": record.id,
                "status": record.status,
                "expires_at": record.expires_at,
                "error_code": record.error_code,
                "deck": deck_data,
                "retryable": record.status == "pending"
                or (record.status == "failed" and record.error_code in RETRYABLE),
            }
        )

    async def storage_usage(self, session: AsyncSession, owner: UUID) -> int:
        reserved = await session.scalar(
            select(func.coalesce(func.sum(DeckUpload.size_bytes), 0)).where(
                DeckUpload.owner_id == owner,
                DeckUpload.status.in_(ACTIVE),
            )
        )
        used = await session.scalar(
            select(func.coalesce(func.sum(Deck.source_size_bytes), 0)).where(Deck.owner_id == owner)
        )
        evidence = await session.scalar(
            select(func.coalesce(func.sum(ActionCheck.storage_bytes), 0)).where(
                ActionCheck.owner_id == owner
            )
        )
        return int(reserved or 0) + int(used or 0) + int(evidence or 0)

    async def limits(self, session: AsyncSession, owner: UUID, size: int) -> None:
        if size > self.settings.max_bytes:
            raise UploadError(
                413, "file_too_large", "Presentation exceeds the configured file size limit."
            )
        if await self.storage_usage(session, owner) + size > self.settings.quota_bytes:
            raise UploadError(
                413,
                "storage_quota_exceeded",
                "This upload would exceed your private storage quota.",
            )
        owned = await session.scalar(
            select(func.count())
            .select_from(DeckUpload)
            .where(
                DeckUpload.owner_id == owner,
                DeckUpload.status.in_(ACTIVE),
            )
        )
        total = await session.scalar(
            select(func.count()).select_from(DeckUpload).where(DeckUpload.status.in_(ACTIVE))
        )
        if (
            int(owned or 0) >= self.settings.owner_concurrency
            or int(total or 0) >= self.settings.global_concurrency
        ):
            raise UploadError(
                429, "upload_capacity", "Upload capacity is temporarily full; retry later.", 30
            )

    async def initialize(self, owner: UUID, key: UUID, metadata: UploadMetadata) -> UploadData:
        async with self.transaction() as session:
            await self.admission_lock(session)
            record = await session.scalar(
                select(DeckUpload).where(
                    DeckUpload.owner_id == owner,
                    DeckUpload.idempotency_key == key,
                )
            )
            if record is not None:
                if any(
                    getattr(record, name) != getattr(metadata, name)
                    for name in ("filename", "title", "size_bytes", "sha256", "source_format")
                ):
                    raise UploadError(
                        409,
                        "idempotency_conflict",
                        "This retry key is already bound to another upload.",
                    )
                if record.status != "failed" or record.error_code not in RETRYABLE:
                    return await self.describe(session, record)
                await self.limits(session, owner, metadata.size_bytes)
                record.status = "pending"
                record.error_code = None
                record.lease_id = None
                record.expires_at = datetime.now(UTC) + timedelta(
                    seconds=self.settings.reservation_seconds
                )
            else:
                await self.limits(session, owner, metadata.size_bytes)
                identifier, revision = uuid4(), str(uuid4())
                record = DeckUpload(
                    id=identifier,
                    owner_id=owner,
                    idempotency_key=key,
                    filename=metadata.filename,
                    source_format=metadata.source_format,
                    title=metadata.title,
                    size_bytes=metadata.size_bytes,
                    sha256=metadata.sha256,
                    source_revision=revision,
                    storage_key=f"sources/{owner}/{identifier}/{revision}/original.{metadata.source_format}",
                    status="pending",
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=self.settings.reservation_seconds),
                )
                session.add(record)
            await session.flush()
            return await self.describe(session, record)

    async def get(self, owner: UUID, identifier: UUID) -> UploadData:
        async with self.transaction() as session:
            return await self.describe(session, await self.owned(session, owner, identifier))

    async def claim(self, owner: UUID, identifier: UUID) -> DeckUpload:
        async with self.transaction() as session:
            record = await self.owned(session, owner, identifier, lock=True)
            if record.status != "pending" or record.expires_at <= datetime.now(UTC):
                raise UploadError(
                    409, "upload_not_pending", "Check the upload status before retrying.", 30
                )
            record.status = "receiving"
            record.lease_id = uuid4()
            record.expires_at = datetime.now(UTC) + timedelta(
                seconds=self.settings.reservation_seconds
            )
            await session.flush()
            return record

    async def finalize(self, record: DeckUpload, version: str, slides: int) -> UploadData:
        async with self.transaction() as session:
            await self.admission_lock(session)
            current = await self.owned(session, record.owner_id, record.id, lock=True)
            if current.status != "receiving" or current.lease_id != record.lease_id:
                raise UploadError(
                    409, "upload_not_pending", "Upload has expired or was interrupted."
                )
            deck = Deck(
                id=record.id,
                owner_id=record.owner_id,
                title=record.title,
                source_key=record.storage_key,
                source_revision=record.source_revision,
                source_sha256=record.sha256,
                source_size_bytes=record.size_bytes,
                source_version_id=version,
                source_slide_count=slides,
                source_format=record.source_format,
            )
            session.add(deck)
            await session.flush()
            current.deck_id = deck.id
            current.status = "complete"
            current.error_code = None
            await session.flush()
            return await self.describe(session, current)

    async def mark_cleanup(self, record: DeckUpload, code: str) -> None:
        async with self.transaction() as session:
            current = await self.owned(session, record.owner_id, record.id, lock=True)
            # A lost DB commit acknowledgement may mean the deck actually committed.
            # Never delete its accepted source, nor touch a newer upload lease.
            if current.status == "receiving" and current.lease_id == record.lease_id:
                current.status = "cleanup"
                current.error_code = code

    async def cleanup(self, identifier: UUID) -> None:
        async with self.transaction() as session:
            current = await session.scalar(
                select(DeckUpload)
                .where(DeckUpload.id == identifier)
                .with_for_update(skip_locked=True)
            )
            if current is None or current.status in {"complete", "failed"}:
                return
            if current.status != "cleanup":
                if current.expires_at > datetime.now(UTC):
                    return
                current.status = "cleanup"
                current.error_code = "upload_expired"
                await session.flush()
            # Keep the row locked while deleting: another process cannot rearm a
            # retry, finalize, or clean the same source concurrently.
            await blocking(lambda: self.storage.remove(current.storage_key))
            current.status = "failed"

    async def reconcile(self) -> None:
        async with self.transaction() as session:
            identifiers = list(
                (
                    await session.scalars(
                        select(DeckUpload.id)
                        .where(
                            (DeckUpload.status == "cleanup")
                            | (
                                DeckUpload.status.in_(("pending", "receiving"))
                                & (DeckUpload.expires_at <= datetime.now(UTC))
                            )
                        )
                        .order_by(DeckUpload.expires_at, DeckUpload.id)
                        .limit(16)
                    )
                ).all()
            )
        for identifier in identifiers:
            try:
                await self.cleanup(identifier)
            except UploadError:
                logger.warning("Upload cleanup deferred; durable reservation retained")

    async def run_reconciliation(self) -> None:
        while True:
            try:
                await self.reconcile()
            except UploadError:
                logger.warning("Upload reconciliation deferred; database unavailable")
            await asyncio.sleep(30)

    async def receive(self, owner: UUID, identifier: UUID, request: Request) -> UploadData:
        # Completed retries return the original durable result without another PUT.
        existing = await self.get(owner, identifier)
        if existing.status == "complete":
            return existing
        async with self.transaction() as session:
            reserved = await self.owned(session, owner, identifier)
            source_format = reserved.source_format
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != SOURCE_TYPES[source_format]
        ):
            raise UploadError(
                415, "unsupported_media_type", "Send the reserved presentation content type."
            )
        record = await self.claim(owner, identifier)
        try:
            async with asyncio.timeout(self.settings.seconds):
                with tempfile.TemporaryDirectory(prefix="buddy-upload-") as directory:
                    path = Path(directory) / ("source." + record.source_format)
                    digest, size = hashlib.sha256(), 0
                    with path.open("xb") as output:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > record.size_bytes or size > self.settings.max_bytes:
                                raise UploadError(
                                    413,
                                    "file_too_large",
                                    "Transferred presentation exceeds its reserved size.",
                                )
                            digest.update(chunk)
                            output.write(chunk)
                    if size != record.size_bytes or digest.hexdigest() != record.sha256:
                        raise UploadError(
                            422,
                            "source_mismatch",
                            "Transferred size or checksum does not match the upload reservation.",
                        )
                    async with self.parser_slot:
                        slides = await blocking(
                            lambda: validate_source(path, record.source_revision, self.settings)
                        )
                    version = await blocking(
                        lambda: self.storage.put(
                            record.storage_key,
                            path,
                            size=size,
                            content_type=SOURCE_TYPES[record.source_format],
                            sha256=record.sha256,
                            revision=record.source_revision,
                        )
                    )
                    return await self.finalize(record, version, slides)
        except (Exception, asyncio.CancelledError) as exc:
            code = (
                exc.code
                if isinstance(exc, UploadError)
                else "upload_timeout"
                if isinstance(exc, TimeoutError)
                else "upload_interrupted"
                if isinstance(exc, (ClientDisconnect, asyncio.CancelledError))
                else "service_unavailable"
            )
            try:
                await asyncio.shield(self.mark_cleanup(record, code))
                await asyncio.shield(self.cleanup(record.id))
            except (UploadError, asyncio.CancelledError):
                # A failed DB transition leaves the existing durable lease for the
                # reconciler; cleanup is forbidden until DB ownership is certain.
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, UploadError):
                raise
            status = 408 if code in {"upload_timeout", "upload_interrupted"} else 503
            raise UploadError(
                status,
                code,
                "Upload did not finish. Check its status and retry with the same key.",
                15,
            ) from None
