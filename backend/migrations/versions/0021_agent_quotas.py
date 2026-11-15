"""Shared bounded agent allowance and provider pacing; no speech storage."""

import sqlalchemy as sa
from alembic import op

revision = "0021_agent_quotas"
down_revision = "0020_pdf_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_quotas",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("last_called_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("calls >= 0", name=op.f("ck_agent_quotas_calls_nonnegative")),
    )


def downgrade() -> None:
    op.drop_table("agent_quotas")
