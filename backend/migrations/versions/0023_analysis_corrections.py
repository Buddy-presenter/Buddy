"""Persist owner corrections as immutable analysis lineage events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_analysis_corrections"
down_revision = "0022_agent_request_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_action_checks_owner_identity", "action_checks", ["id", "owner_id"]
    )
    op.add_column(
        "action_validations",
        sa.Column("evidence_source_validation_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_action_validations_evidence_source_owner",
        "action_validations",
        "action_checks",
        ["evidence_source_validation_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "analysis_corrections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("deck_id", sa.Uuid(), nullable=False),
        sa.Column("source_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("result_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("action_instance_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("source_action_sha256", sa.String(64), nullable=False),
        sa.Column("result_action_sha256", sa.String(64)),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["source_analysis_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_analysis_id", "deck_id", "owner_id"],
            ["analysis_versions.id", "analysis_versions.deck_id", "analysis_versions.owner_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_id", "request_id"),
        sa.CheckConstraint("operation IN ('set_region','disable')", name="operation"),
        sa.CheckConstraint("result_version > source_version", name="version_order"),
    )
    for column in ("owner_id", "deck_id", "source_analysis_id", "result_analysis_id"):
        op.create_index(f"ix_analysis_corrections_{column}", "analysis_corrections", [column])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM analysis_corrections")):
        raise RuntimeError("Preserve owner correction history before downgrading")
    op.drop_table("analysis_corrections")
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM action_validations WHERE evidence_source_validation_id IS NOT NULL"
        )
    ):
        raise RuntimeError("Preserve copied validation evidence before downgrading")
    op.drop_constraint(
        "fk_action_validations_evidence_source_owner", "action_validations", type_="foreignkey"
    )
    op.drop_column("action_validations", "evidence_source_validation_id")
    op.drop_constraint("uq_action_checks_owner_identity", "action_checks", type_="unique")
