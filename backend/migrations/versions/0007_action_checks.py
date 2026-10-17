"""Owner-scoped action-check results and private evidence references."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_action_checks"
down_revision = "0006_pdf_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("retry_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(
            ["deck_id", "owner_id"], ["decks.id", "decks.owner_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("owner_id", "retry_key"),
        sa.CheckConstraint("status IN ('pending','complete','cleanup','failed')", name="status"),
        sa.CheckConstraint(
            "storage_bytes >= 0 AND storage_bytes <= 33554432", name="storage_bytes"
        ),
    )
    for name in ("owner_id", "deck_id", "status"):
        op.create_index(f"ix_action_checks_{name}", "action_checks", [name])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM action_checks")):
        raise RuntimeError("Preserve action-check evidence before downgrading")
    op.drop_table("action_checks")
