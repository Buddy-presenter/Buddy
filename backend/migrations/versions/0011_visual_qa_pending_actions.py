"""Persist image-subregion actions as candidates for independent visual QA."""

import sqlalchemy as sa
from alembic import op

revision = "0011_visual_qa_pending"
down_revision = "0010_analysis_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slide_actions",
        sa.Column("visual_qa_pending", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Analysis migration is forward-only; restore a verified backup "
        "or use a reviewed forward migration."
    )
