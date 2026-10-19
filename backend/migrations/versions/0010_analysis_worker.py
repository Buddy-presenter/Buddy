"""Record final and explicitly partial PDF analysis job states."""

from alembic import op

revision = "0010_analysis_worker"
down_revision = "0009_pdf_workers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_processing_jobs_status"), "processing_jobs", type_="check")
    op.create_check_constraint(
        "status",
        "processing_jobs",
        "status IN ('pending','running','succeeded','failed','queued','retry_wait',"
        "'metadata_ready','analysis_ready','analysis_partial','cancelled','dead_letter')",
    )


def downgrade() -> None:
    raise RuntimeError("Retain analysis job states; use a reviewed forward migration.")
