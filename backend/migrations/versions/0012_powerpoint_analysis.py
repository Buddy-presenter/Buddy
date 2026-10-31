"""Bind analysis, action and validation records to their source format."""

import sqlalchemy as sa
from alembic import op

revision = "0012_powerpoint_analysis"
down_revision = "0011_visual_qa_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("analysis_versions", "slide_actions", "action_validations"):
        op.add_column(
            table, sa.Column("source_format", sa.String(8), server_default="pdf", nullable=False)
        )
        # This transactional migration adds provenance only; existing content hashes
        # and immutable source/action documents are deliberately untouched.
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER immutable_revision")
        op.execute(
            f"UPDATE {table} AS r SET source_format=d.source_format "
            "FROM decks AS d WHERE r.deck_id=d.id"
        )
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER immutable_revision")
        op.create_check_constraint(
            op.f(f"ck_{table}_source_format"), table, "source_format IN ('pdf','pptx')"
        )
    op.drop_constraint(op.f("ck_slide_actions_content_type"), "slide_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_slide_actions_content_type"),
        "slide_actions",
        "(source_format='pdf' AND action_type IN ('zoom_region','highlight_region')) OR "
        "(source_format='pptx' AND action_type IN ('highlight_region','draw_rectangle',"
        "'draw_oval','draw_underline','draw_arrow','callout'))",
    )
    for table in ("decks", "analysis_versions", "slide_actions"):
        op.create_unique_constraint(
            f"uq_{table}_format_identity", table, ["id", "owner_id", "source_format"]
        )
    for table, parent, key in (
        ("analysis_versions", "decks", "deck_id"),
        ("slide_actions", "analysis_versions", "analysis_id"),
        ("action_validations", "slide_actions", "action_id"),
    ):
        op.create_foreign_key(
            f"fk_{table}_source_format",
            table,
            parent,
            [key, "owner_id", "source_format"],
            ["id", "owner_id", "source_format"],
            ondelete="RESTRICT",
        )
    op.execute("""CREATE FUNCTION legacy_analysis_format() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.publication = 'legacy' THEN
        SELECT source_format INTO NEW.source_format FROM decks WHERE id=NEW.deck_id;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER legacy_analysis_format BEFORE INSERT ON analysis_versions
      FOR EACH ROW EXECUTE FUNCTION legacy_analysis_format()""")


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration: restore a verified backup or apply a reviewed repair."
    )
