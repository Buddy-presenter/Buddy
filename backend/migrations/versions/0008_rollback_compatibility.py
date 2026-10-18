"""Retain the legacy analysis contract during the staged application rollout."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_rollback_compatibility"
down_revision = "0007_action_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Repair forward rather than rewriting a revision already used by developers.
    # The complete upgrade is transactional: readers never observe the temporary
    # removal/recreation of the column or this trigger.
    op.add_column(
        "analysis_versions",
        sa.Column("graph", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.execute("DROP TRIGGER immutable_revision ON analysis_versions")
    op.execute("""UPDATE analysis_versions AS analysis SET graph = archive.payload
        FROM legacy_analysis_payloads AS archive WHERE analysis.id = archive.analysis_id""")
    op.execute("""CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE
        ON analysis_versions FOR EACH ROW EXECUTE FUNCTION analysis_immutable()""")
    op.execute("""CREATE FUNCTION prepare_legacy_analysis_graph() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.publication = 'legacy' AND NEW.summary_status = 'unavailable'
             AND NEW.summary_reason IS NULL THEN
            NEW.summary_reason := 'Legacy analysis requires reprocessing.';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER prepare_legacy_graph BEFORE INSERT ON analysis_versions
        FOR EACH ROW EXECUTE FUNCTION prepare_legacy_analysis_graph()""")
    # A legacy writer may resume after rollback. Preserve its original payload as
    # well as the compatibility column, without interpreting it as typed analysis.
    op.execute("""CREATE FUNCTION archive_legacy_analysis_graph() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.publication = 'legacy' THEN
            INSERT INTO legacy_analysis_payloads(analysis_id, payload)
            VALUES (NEW.id, NEW.graph);
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER archive_legacy_graph AFTER INSERT ON analysis_versions
        FOR EACH ROW EXECUTE FUNCTION archive_legacy_analysis_graph()""")


def downgrade() -> None:
    raise RuntimeError("Compatibility data must be retained; use a reviewed forward migration.")
