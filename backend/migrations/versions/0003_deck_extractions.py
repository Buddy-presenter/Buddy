"""Versioned extraction records, separate from provider analysis and job success."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_extractions"
down_revision = "0002_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deck_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("extractor_version", sa.String(16), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("deck_id", "source_revision", "extractor_version"),
        sa.CheckConstraint("length(source_revision) > 0", name="source_revision_nonempty"),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="source_sha256"),
        sa.CheckConstraint("extractor_version = '1'", name="extractor_version"),
    )
    op.create_index("ix_deck_extractions_owner_id", "deck_extractions", ["owner_id"])
    op.create_index("ix_deck_extractions_deck_id", "deck_extractions", ["deck_id"])


def downgrade() -> None:
    op.drop_table("deck_extractions")
