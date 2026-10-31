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
        UniqueConstraint("id", "owner_id", "source_format", name="uq_decks_format_identity"),
        CheckConstraint("length(title) > 0", name="title_nonempty"),
        CheckConstraint("source_format IN ('pptx','pdf')", name="source_format"),
    )
    title: Mapped[str] = mapped_column(String(256))
    source_key: Mapped[str | None] = mapped_column(String(1024))
    source_revision: Mapped[str | None] = mapped_column(String(128))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_version_id: Mapped[str | None] = mapped_column(String(1024))
    source_slide_count: Mapped[int | None] = mapped_column()
    source_format: Mapped[str] = mapped_column(String(8), server_default="pptx")


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
        CheckConstraint("source_format IN ('pptx','pdf')", name="source_format"),
    )
    idempotency_key: Mapped[UUID] = mapped_column()
    filename: Mapped[str] = mapped_column(String(255))
    source_format: Mapped[str] = mapped_column(String(8), server_default="pptx")
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


class ActionCheck(OwnedRecord):
    __tablename__ = "action_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("owner_id", "retry_key"),
        CheckConstraint("status IN ('pending','complete','cleanup','failed')", name="status"),
        CheckConstraint("storage_bytes >= 0 AND storage_bytes <= 33554432", name="storage_bytes"),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    retry_key: Mapped[UUID] = mapped_column()
    request_sha256: Mapped[str] = mapped_column(String(64))
    storage_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    status: Mapped[str] = mapped_column(String(16), server_default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")


class DeckExtraction(OwnedRecord):
    __tablename__ = "deck_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("deck_id", "source_revision", "extractor_version"),
        UniqueConstraint("id", "deck_id", "owner_id"),
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
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','queued','retry_wait',"
            "'metadata_ready','analysis_ready','analysis_partial','cancelled','dead_letter')",
            name="status",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        UniqueConstraint("deck_id", "source_revision"),
        ForeignKeyConstraint(
            ["extraction_id", "deck_id", "owner_id"],
            ["deck_extractions.id", "deck_extractions.deck_id", "deck_extractions.owner_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation >= 0 AND progress >= 0 AND progress <= 100", name="progress"),
        CheckConstraint(
            "stage_state IN ('pending','running','succeeded','failed','cancelled')",
            name="stage_state",
        ),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")
    source_revision: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(server_default="0")
    stage: Mapped[str] = mapped_column(String(32), server_default="pdf_metadata.v1")
    stage_state: Mapped[str] = mapped_column(String(16), server_default="pending")
    progress: Mapped[int] = mapped_column(server_default="0")
    lease_id: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error_code: Mapped[str | None] = mapped_column(String(64))
    extraction_id: Mapped[UUID | None] = mapped_column()


class JobOutbox(Base):
    __tablename__ = "job_outbox"
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    publish_attempts: Mapped[int] = mapped_column(server_default="0")


class AnalysisVersion(OwnedRecord):
    __tablename__ = "analysis_versions"
    source_format: Mapped[str] = mapped_column(String(8), server_default="pdf")
    __table_args__ = (
        CheckConstraint("source_format IN ('pdf','pptx')", name="source_format"),
        UniqueConstraint(
            "id", "owner_id", "source_format", name="uq_analysis_versions_format_identity"
        ),
        ForeignKeyConstraint(
            ["deck_id", "owner_id", "source_format"],
            ["decks.id", "decks.owner_id", "decks.source_format"],
            name="fk_analysis_versions_source_format",
            ondelete="RESTRICT",
        ),
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
        ForeignKeyConstraint(
            ["extraction_id", "deck_id", "owner_id"],
            ["deck_extractions.id", "deck_extractions.deck_id", "deck_extractions.owner_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "publication IN ('legacy','partial','awaiting_validation','published')",
            name="publication",
        ),
        CheckConstraint(
            "(publication = 'legacy' AND provenance IS NULL AND content_sha256 IS NULL) OR "
            "(publication <> 'legacy' AND provenance IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND extraction_id IS NOT NULL AND job_id IS NOT NULL)",
            name="typed_lineage",
        ),
    )
    deck_id: Mapped[UUID] = mapped_column(index=True)
    job_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int] = mapped_column()
    # Compatibility only; typed results never interpret or expose this payload.
    graph: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    extraction_id: Mapped[UUID | None] = mapped_column()
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    publication: Mapped[str] = mapped_column(String(24), server_default="legacy")
    summary_status: Mapped[str] = mapped_column(String(16), server_default="unavailable")
    summary_text: Mapped[str | None] = mapped_column()
    summary_reason: Mapped[str | None] = mapped_column(String(512))


class LegacyAnalysisPayload(Base):
    """Legacy compatibility archive; never returned as current typed analysis."""

    __tablename__ = "legacy_analysis_payloads"
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AnalysisSlide(OwnedRecord):
    __tablename__ = "analysis_slides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["analysis_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("analysis_id", "slide_id"),
        UniqueConstraint("analysis_id", "position", name="uq_analysis_slides_position"),
        UniqueConstraint("id", "analysis_id", "deck_id", "owner_id"),
        CheckConstraint("position >= 0 AND position < 500", name="position"),
        CheckConstraint(
            "summary_status IN ('pending','available','unavailable')", name="summary_status"
        ),
    )
    analysis_id: Mapped[UUID] = mapped_column(index=True)
    deck_id: Mapped[UUID] = mapped_column()
    slide_id: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column()
    summary_status: Mapped[str] = mapped_column(String(16))
    summary_text: Mapped[str | None] = mapped_column()
    summary_reason: Mapped[str | None] = mapped_column(String(512))


class SlideAction(OwnedRecord):
    __tablename__ = "slide_actions"
    source_format: Mapped[str] = mapped_column(String(8), server_default="pdf")
    __table_args__ = (
        CheckConstraint("source_format IN ('pdf','pptx')", name="source_format"),
        UniqueConstraint(
            "id", "owner_id", "source_format", name="uq_slide_actions_format_identity"
        ),
        ForeignKeyConstraint(
            ["analysis_id", "owner_id", "source_format"],
            [
                "analysis_versions.id",
                "analysis_versions.owner_id",
                "analysis_versions.source_format",
            ],
            name="fk_slide_actions_source_format",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["slide_record_id", "analysis_id", "deck_id", "owner_id"],
            [
                "analysis_slides.id",
                "analysis_slides.analysis_id",
                "analysis_slides.deck_id",
                "analysis_slides.owner_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("analysis_id", "instance_id"),
        UniqueConstraint("slide_record_id", "position"),
        UniqueConstraint("id", "analysis_id", "deck_id", "owner_id"),
        CheckConstraint(
            "(source_format = 'pdf' AND action_type IN ('zoom_region','highlight_region')) OR "
            "(source_format = 'pptx' AND action_type IN ('highlight_region','draw_rectangle',"
            "'draw_oval','draw_underline','draw_arrow','callout','draw_line','filled_rectangle',"
            "'filled_oval','text_box','draw_rounded_rectangle','draw_diamond','draw_triangle',"
            "'draw_hexagon','draw_star'))",
            name="content_type",
        ),
        CheckConstraint("position >= 0 AND position < 100", name="position"),
    )
    slide_record_id: Mapped[UUID] = mapped_column()
    analysis_id: Mapped[UUID] = mapped_column(index=True)
    deck_id: Mapped[UUID] = mapped_column()
    instance_id: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column()
    catalog_version: Mapped[str] = mapped_column(String(16))
    action_type: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(512))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    visual_qa_pending: Mapped[bool] = mapped_column(server_default="false")


class ActionValidation(OwnedRecord):
    __tablename__ = "action_validations"
    source_format: Mapped[str] = mapped_column(String(8), server_default="pdf")
    __table_args__ = (
        CheckConstraint("source_format IN ('pdf','pptx')", name="source_format"),
        ForeignKeyConstraint(
            ["action_id", "owner_id", "source_format"],
            ["slide_actions.id", "slide_actions.owner_id", "slide_actions.source_format"],
            name="fk_action_validations_source_format",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_id", "analysis_id", "deck_id", "owner_id"],
            [
                "slide_actions.id",
                "slide_actions.analysis_id",
                "slide_actions.deck_id",
                "slide_actions.owner_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("action_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence"),
        CheckConstraint(
            "state IN ('candidate','validated','rejected','needs-review')", name="state"
        ),
    )
    action_id: Mapped[UUID] = mapped_column(index=True)
    analysis_id: Mapped[UUID] = mapped_column(index=True)
    deck_id: Mapped[UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column()
    state: Mapped[str] = mapped_column(String(16))
    document: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


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
