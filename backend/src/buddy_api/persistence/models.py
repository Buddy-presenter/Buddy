"""Durable records and database-enforced account/deck consistency."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("length(subject) > 0", name="subject_nonempty"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OwnedRecord(Base):
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Deck(OwnedRecord):
    __tablename__ = "decks"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        CheckConstraint("length(title) > 0", name="title_nonempty"),
    )
    title: Mapped[str] = mapped_column(String(256))
    source_key: Mapped[str | None] = mapped_column(String(1024))
    source_revision: Mapped[str | None] = mapped_column(String(128))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_version_id: Mapped[str | None] = mapped_column(String(1024))
    source_slide_count: Mapped[int | None] = mapped_column()


class DeckUpload(OwnedRecord):
    __tablename__ = "deck_uploads"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        CheckConstraint(
            "status IN ('pending','receiving','cleanup','failed','complete')", name="status"
        ),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 67108864", name="size_bounds"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("status != 'complete' OR deck_id IS NOT NULL", name="completed_deck"),
    )
    idempotency_key: Mapped[UUID] = mapped_column()
    filename: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(256))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str] = mapped_column(String(128), unique=True)
    storage_key: Mapped[str] = mapped_column(String(1024), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    lease_id: Mapped[UUID | None] = mapped_column()
    deck_id: Mapped[UUID | None] = mapped_column()


class RenderedAsset(OwnedRecord):
    __tablename__ = "rendered_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("length(storage_key) > 0", name="storage_key_nonempty"),
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    storage_key: Mapped[str] = mapped_column(String(1024), unique=True)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column()


class DeckExtraction(OwnedRecord):
    __tablename__ = "deck_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("deck_id", "source_revision", "extractor_version"),
        CheckConstraint("length(source_revision) > 0", name="source_revision_nonempty"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="source_sha256"),
        CheckConstraint("extractor_version = '1'", name="extractor_version"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    source_revision: Mapped[str] = mapped_column(String(128))
    source_sha256: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(16))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ProcessingJob(OwnedRecord):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("id", "deck_id", "owner_id"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")


class AnalysisVersion(OwnedRecord):
    __tablename__ = "analysis_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["job_id", "deck_id", "owner_id"],
            ["processing_jobs.id", "processing_jobs.deck_id", "processing_jobs.owner_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "deck_id", "owner_id"),
        UniqueConstraint("deck_id", "version"),
        CheckConstraint("version > 0", name="version_positive"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    job_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column()
    graph: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PresentationSession(OwnedRecord):
    __tablename__ = "presentation_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["analysis_version_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('created','active','ended')", name="status"),
        CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    analysis_version_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="created")
    state_version: Mapped[int] = mapped_column(server_default="0")
