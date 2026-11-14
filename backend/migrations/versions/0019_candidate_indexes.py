"""Derived owner-scoped embedding cache for immutable presentation analysis."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_candidate_indexes"
down_revision = "0018_session_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "embedding_quotas",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("calls", sa.Integer(), nullable=False),
    )
    op.create_table(
        "candidate_indexes",
        sa.Column("analysis_id", sa.Uuid(), primary_key=True),
        sa.Column("space", sa.String(64), primary_key=True),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("vectors", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_candidate_indexes_owner_id", "candidate_indexes", ["owner_id"])


def downgrade() -> None:
    op.drop_table("embedding_quotas")
    op.drop_table("candidate_indexes")
