"""Persist rendered-asset ownership before any private asset endpoint is enabled."""

import sqlalchemy as sa
from alembic import op

revision = "0002_assets"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rendered_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False, unique=True),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("length(storage_key) > 0", name="storage_key_nonempty"),
        sa.CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    )
    op.create_index("ix_rendered_assets_owner_id", "rendered_assets", ["owner_id"])
    op.create_index("ix_rendered_assets_deck_id", "rendered_assets", ["deck_id"])


def downgrade() -> None:
    op.drop_table("rendered_assets")
