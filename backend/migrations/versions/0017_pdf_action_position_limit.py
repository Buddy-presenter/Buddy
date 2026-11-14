"""Align persisted PDF action positions with the v2 contract limit."""

import sqlalchemy as sa
from alembic import op

revision = "0017_pdf_action_position_limit"
down_revision = "0016_tombstone_analysis_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_slide_actions_position"), "slide_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_slide_actions_position"),
        "slide_actions",
        "position >= 0 AND ((source_format = 'pdf' AND position < 1000) OR "
        "(source_format = 'pptx' AND position < 100))",
    )


def downgrade() -> None:
    # Never discard persisted actions merely to restore the old schema limit.
    connection = op.get_bind()
    has_expanded_positions = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM slide_actions "
            "WHERE source_format = 'pdf' AND position >= 100)"
        )
    )
    if has_expanded_positions:
        raise RuntimeError(
            "Cannot restore the 100-action position limit while expanded PDF actions exist."
        )

    op.drop_constraint(op.f("ck_slide_actions_position"), "slide_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_slide_actions_position"),
        "slide_actions",
        "position >= 0 AND position < 100",
    )
