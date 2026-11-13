"""Bounded durable session-event replay and idempotent command receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_session_events"
down_revision = "0017_pdf_action_position_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "presentation_sessions",
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_presentation_sessions_event_sequence_nonnegative"),
        "presentation_sessions",
        "event_sequence >= 0",
    )
    op.create_table(
        "session_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=True),
        sa.Column("command_sha256", sa.String(64), nullable=True),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["presentation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_session_events_sequence"),
        sa.UniqueConstraint("session_id", "command_id", name="uq_session_events_command"),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_session_events_sequence_positive")),
    )
    op.create_index("ix_session_events_session_id", "session_events", ["session_id"])
    op.create_index("ix_session_events_expires_at", "session_events", ["expires_at"])


def downgrade() -> None:
    op.drop_table("session_events")
    op.drop_constraint(
        op.f("ck_presentation_sessions_event_sequence_nonnegative"),
        "presentation_sessions",
        type_="check",
    )
    op.drop_column("presentation_sessions", "event_sequence")
