"""Private immutable deck sources and durable upload reservations/reconciliation."""

import sqlalchemy as sa
from alembic import op

revision = "0004_uploads"
down_revision = "0003_extractions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("source_revision", sa.String(128)),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("source_size_bytes", sa.BigInteger()),
        sa.Column("source_version_id", sa.String(1024)),
        sa.Column("source_slide_count", sa.Integer()),
    ):
        op.add_column("decks", column)
    op.create_table(
        "deck_uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False, unique=True),
        sa.Column("storage_key", sa.String(1024), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("deck_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
        sa.ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('pending','receiving','cleanup','failed','complete')", name="status"
        ),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 67108864", name="size_bounds"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        sa.CheckConstraint("status != 'complete' OR deck_id IS NOT NULL", name="completed_deck"),
    )
    op.create_index("ix_deck_uploads_owner_id", "deck_uploads", ["owner_id"])
    op.create_index("ix_deck_uploads_status", "deck_uploads", ["status"])
    op.create_index("ix_deck_uploads_expires_at", "deck_uploads", ["expires_at"])


def downgrade() -> None:
    op.drop_table("deck_uploads")
    for name in (
        "source_slide_count",
        "source_version_id",
        "source_size_bytes",
        "source_sha256",
        "source_revision",
    ):
        op.drop_column("decks", name)
