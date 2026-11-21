"""Durable trusted preflight requests; no client-asserted results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_preflight_publication"
down_revision = "0023_analysis_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("presentation_sessions", sa.Column("runtime_context", JSONB(), nullable=True))
    op.add_column(
        "action_checks",
        sa.Column("check_kind", sa.String(16), nullable=False, server_default="execution"),
    )
    op.add_column("action_checks", sa.Column("request_data", JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_action_checks_check_kind", "action_checks", "check_kind IN ('execution','preflight')"
    )


def downgrade() -> None:
    op.drop_column("presentation_sessions", "runtime_context")
    op.drop_constraint("ck_action_checks_check_kind", "action_checks", type_="check")
    op.drop_column("action_checks", "request_data")
    op.drop_column("action_checks", "check_kind")
