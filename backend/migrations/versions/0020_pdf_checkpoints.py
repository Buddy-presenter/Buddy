"""Durable PDF provider and per-page grounding checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_pdf_checkpoints"
down_revision = "0019_candidate_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdf_processing_checkpoints",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("binding", sa.String(64), nullable=False),
        sa.Column("generated", postgresql.JSONB(), nullable=False),
        sa.Column("pages", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "deck_id", "owner_id"],
            ["processing_jobs.id", "processing_jobs.deck_id", "processing_jobs.owner_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "deck_id", "owner_id"],
            ["deck_extractions.id", "deck_extractions.deck_id", "deck_extractions.owner_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_pdf_processing_checkpoints_owner_id", "pdf_processing_checkpoints", ["owner_id"]
    )


def downgrade() -> None:
    op.drop_table("pdf_processing_checkpoints")
