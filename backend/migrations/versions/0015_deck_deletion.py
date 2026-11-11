"""Add durable, retryable deck deletion requests."""

import sqlalchemy as sa
from alembic import op

revision = "0015_deck_deletion"
down_revision = "0014_powerpoint_zoom"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "decks", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_decks_deletion_requested_at", "decks", ["deletion_requested_at"])


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration; restore a verified backup or apply a reviewed repair."
    )
