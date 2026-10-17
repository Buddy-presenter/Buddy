"""versioned_analysis_records

Revision ID: 95b10c10806a
Revises: 0003_extractions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "95b10c10806a"
down_revision = "0003_extractions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive transition: retain all analysis/session IDs and archive old payloads first.
    op.create_table(
        "analysis_slides",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("slide_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("summary_status", sa.String(length=16), nullable=False),
        sa.Column("summary_text", sa.String(), nullable=True),
        sa.Column("summary_reason", sa.String(length=512), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "summary_status IN ('pending','available','unavailable')",
            name=op.f("ck_analysis_slides_summary_status"),
        ),
        sa.CheckConstraint(
            "position >= 0 AND position < 500", name=op.f("ck_analysis_slides_position")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            name=op.f("fk_analysis_slides_analysis_id_analysis_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_analysis_slides_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_slides")),
        sa.UniqueConstraint("analysis_id", "position", name="uq_analysis_slides_position"),
        sa.UniqueConstraint("analysis_id", "slide_id", name=op.f("uq_analysis_slides_analysis_id")),
        sa.UniqueConstraint(
            "id", "analysis_id", "deck_id", "owner_id", name=op.f("uq_analysis_slides_id")
        ),
    )
    op.create_index(
        op.f("ix_analysis_slides_analysis_id"), "analysis_slides", ["analysis_id"], unique=False
    )
    op.create_index(
        op.f("ix_analysis_slides_owner_id"), "analysis_slides", ["owner_id"], unique=False
    )
    op.create_table(
        "legacy_analysis_payloads",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analysis_versions.id"],
            name=op.f("fk_legacy_analysis_payloads_analysis_id_analysis_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("analysis_id", name=op.f("pk_legacy_analysis_payloads")),
    )
    op.create_table(
        "slide_actions",
        sa.Column("slide_record_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=16), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=512), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_type IN ('zoom_region','highlight_region')",
            name=op.f("ck_slide_actions_content_type"),
        ),
        sa.CheckConstraint(
            "position >= 0 AND position < 100", name=op.f("ck_slide_actions_position")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_slide_actions_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["slide_record_id", "analysis_id", "deck_id", "owner_id"],
            [
                "analysis_slides.id",
                "analysis_slides.analysis_id",
                "analysis_slides.deck_id",
                "analysis_slides.owner_id",
            ],
            name=op.f("fk_slide_actions_slide_record_id_analysis_slides"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slide_actions")),
        sa.UniqueConstraint(
            "analysis_id", "instance_id", name=op.f("uq_slide_actions_analysis_id")
        ),
        sa.UniqueConstraint(
            "id", "analysis_id", "deck_id", "owner_id", name=op.f("uq_slide_actions_id")
        ),
        sa.UniqueConstraint(
            "slide_record_id", "position", name=op.f("uq_slide_actions_slide_record_id")
        ),
    )
    op.create_index(
        op.f("ix_slide_actions_analysis_id"), "slide_actions", ["analysis_id"], unique=False
    )
    op.create_index(op.f("ix_slide_actions_owner_id"), "slide_actions", ["owner_id"], unique=False)
    op.create_table(
        "action_validations",
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('candidate','validated','rejected','needs-review')",
            name=op.f("ck_action_validations_state"),
        ),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_action_validations_sequence")),
        sa.ForeignKeyConstraint(
            ["action_id", "analysis_id", "deck_id", "owner_id"],
            [
                "slide_actions.id",
                "slide_actions.analysis_id",
                "slide_actions.deck_id",
                "slide_actions.owner_id",
            ],
            name=op.f("fk_action_validations_action_id_slide_actions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_action_validations_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_validations")),
        sa.UniqueConstraint("action_id", "sequence", name=op.f("uq_action_validations_action_id")),
    )
    op.create_index(
        op.f("ix_action_validations_action_id"), "action_validations", ["action_id"], unique=False
    )
    op.create_index(
        op.f("ix_action_validations_analysis_id"),
        "action_validations",
        ["analysis_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_action_validations_owner_id"), "action_validations", ["owner_id"], unique=False
    )
    op.add_column("analysis_versions", sa.Column("extraction_id", sa.Uuid(), nullable=True))
    op.add_column(
        "analysis_versions",
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "analysis_versions", sa.Column("content_sha256", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "analysis_versions",
        sa.Column("publication", sa.String(length=24), server_default="legacy", nullable=False),
    )
    op.add_column(
        "analysis_versions",
        sa.Column(
            "summary_status", sa.String(length=16), server_default="unavailable", nullable=False
        ),
    )
    op.add_column("analysis_versions", sa.Column("summary_text", sa.String(), nullable=True))
    op.add_column(
        "analysis_versions", sa.Column("summary_reason", sa.String(length=512), nullable=True)
    )
    op.execute(
        "INSERT INTO legacy_analysis_payloads (analysis_id, payload) "
        "SELECT id, graph FROM analysis_versions"
    )
    op.execute(
        "UPDATE analysis_versions SET summary_reason = 'Legacy analysis requires reprocessing.'"
    )
    op.drop_column("analysis_versions", "graph")
    op.create_unique_constraint(
        op.f("uq_deck_extractions_id"), "deck_extractions", ["id", "deck_id", "owner_id"]
    )
    op.create_foreign_key(
        op.f("fk_analysis_versions_extraction_id_deck_extractions"),
        "analysis_versions",
        "deck_extractions",
        ["extraction_id", "deck_id", "owner_id"],
        ["id", "deck_id", "owner_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_analysis_versions_publication"),
        "analysis_versions",
        "publication IN ('legacy','partial','awaiting_validation','published')",
    )
    op.create_check_constraint(
        op.f("ck_analysis_versions_typed_lineage"),
        "analysis_versions",
        "(publication = 'legacy' AND provenance IS NULL AND content_sha256 IS NULL) OR "
        "(publication <> 'legacy' AND provenance IS NOT NULL AND content_sha256 IS NOT NULL "
        "AND extraction_id IS NOT NULL AND job_id IS NOT NULL)",
    )
    op.execute("""CREATE FUNCTION analysis_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Analysis revision is immutable' USING ERRCODE = '23514';
      END IF;
      IF TG_TABLE_NAME = 'analysis_versions' THEN
        IF (to_jsonb(NEW) - 'publication') IS DISTINCT FROM (to_jsonb(OLD) - 'publication') THEN
          RAISE EXCEPTION 'Analysis revision is immutable' USING ERRCODE = '23514';
        END IF;
      ELSE
        RAISE EXCEPTION 'Analysis record is immutable' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END $$""")
    for table in (
        "analysis_versions",
        "analysis_slides",
        "slide_actions",
        "action_validations",
        "legacy_analysis_payloads",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION analysis_immutable()"
        )


def downgrade() -> None:
    raise RuntimeError(
        "Analysis migration is forward-only; restore a verified backup "
        "or use a reviewed forward migration."
    )
