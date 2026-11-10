"""Persist versioned presentation state in durable sessions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_session_state"
down_revision = "0013_powerpoint_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "presentation_sessions",
        sa.Column("active_slide_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "presentation_sessions",
        sa.Column("focus_view", sa.String(length=24), server_default="slide", nullable=False),
    )
    op.add_column(
        "presentation_sessions",
        sa.Column("focus_region", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "presentation_sessions",
        sa.Column("autonomy_mode", sa.String(length=16), server_default="manual", nullable=False),
    )
    op.add_column(
        "presentation_sessions",
        sa.Column("last_accepted_action_id", sa.String(length=128), nullable=True),
    )
    # Preserve a recoverable slide for sessions created by the earlier generic API.
    op.execute(
        """
        UPDATE presentation_sessions AS session
        SET active_slide_id = first_slide.slide_id
        FROM (
            SELECT DISTINCT ON (analysis_id) analysis_id, slide_id
            FROM analysis_slides
            ORDER BY analysis_id, position, id
        ) AS first_slide
        WHERE session.analysis_version_id = first_slide.analysis_id
          AND session.active_slide_id IS NULL
        """
    )
    op.create_foreign_key(
        op.f("fk_presentation_sessions_active_slide"),
        "presentation_sessions",
        "analysis_slides",
        ["analysis_version_id", "active_slide_id"],
        ["analysis_id", "slide_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_presentation_sessions_last_action"),
        "presentation_sessions",
        "slide_actions",
        ["analysis_version_id", "last_accepted_action_id"],
        ["analysis_id", "instance_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_presentation_sessions_focus_view"),
        "presentation_sessions",
        "focus_view IN ('slide','zoom_region','highlight_region')",
    )
    op.create_check_constraint(
        op.f("ck_presentation_sessions_focus_region_consistency"),
        "presentation_sessions",
        "(focus_view = 'slide' AND focus_region IS NULL) OR "
        "(focus_view IN ('zoom_region','highlight_region') AND focus_region IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_presentation_sessions_autonomy_mode"),
        "presentation_sessions",
        "autonomy_mode IN ('manual','assisted','autonomous')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration; restore a verified backup or apply a reviewed repair."
    )
