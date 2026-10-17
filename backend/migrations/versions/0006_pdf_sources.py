"""Record source formats without changing existing PPTX sources."""

import sqlalchemy as sa
from alembic import op

revision = "0006_pdf_sources"
down_revision = "95b10c10806a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("decks", "deck_uploads"):
        op.add_column(
            table, sa.Column("source_format", sa.String(8), nullable=False, server_default="pptx")
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_source_format"), table, "source_format IN ('pptx','pdf')"
        )


def downgrade() -> None:
    # Refuse to discard format identity while PDF records still exist.
    connection = op.get_bind()
    for table in ("decks", "deck_uploads"):
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE source_format='pdf'")):
            raise RuntimeError("Remove PDF records before downgrading source-format support")
    for table in ("deck_uploads", "decks"):
        op.drop_constraint(op.f(f"ck_{table}_source_format"), table, type_="check")
        op.drop_column(table, "source_format")
