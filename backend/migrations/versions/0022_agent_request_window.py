"""Rolling provider admission timestamps; no speech or slide content."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022_agent_request_window"
down_revision = "0021_agent_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_quotas",
        sa.Column("request_times", JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("agent_quotas", sa.Column("blocked_until", sa.DateTime(timezone=True)))
    # The old row retains only its latest call, not the preceding minute. Avoid
    # granting a fresh burst on top of unknown pre-upgrade provider usage.
    op.execute(
        "UPDATE agent_quotas SET blocked_until = last_called_at + INTERVAL '60 seconds' "
        "WHERE last_called_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("agent_quotas", "blocked_until")
    op.drop_column("agent_quotas", "request_times")
