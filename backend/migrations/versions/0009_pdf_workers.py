"""Durable PDF jobs and transactional publish outbox; preserve legacy records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0009_pdf_workers"
down_revision = "0008_rollback_compatibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_processing_jobs_status"), "processing_jobs", type_="check")
    op.create_check_constraint(
        "status",
        "processing_jobs",
        "status IN ('pending','running','succeeded','failed','queued','retry_wait',"
        "'metadata_ready','cancelled','dead_letter')",
    )
    for column in (
        sa.Column("source_revision", sa.String(128)),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="pdf_metadata.v1"),
        sa.Column("stage_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", pg.UUID()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column(
            "not_before", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("extraction_id", pg.UUID()),
    ):
        op.add_column("processing_jobs", column)
    op.create_unique_constraint(None, "processing_jobs", ["deck_id", "source_revision"])
    op.create_foreign_key(
        None,
        "processing_jobs",
        "deck_extractions",
        ["extraction_id", "deck_id", "owner_id"],
        ["id", "deck_id", "owner_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "progress", "processing_jobs", "generation >= 0 AND progress >= 0 AND progress <= 100"
    )
    op.create_check_constraint(
        "stage_state",
        "processing_jobs",
        "stage_state IN ('pending','running','succeeded','failed','cancelled')",
    )
    op.create_table(
        "job_outbox",
        sa.Column(
            "job_id",
            pg.UUID(),
            sa.ForeignKey("processing_jobs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_job_outbox_available_at", "job_outbox", ["available_at"])


def downgrade() -> None:
    raise RuntimeError("Retain durable jobs; use a reviewed forward migration.")
