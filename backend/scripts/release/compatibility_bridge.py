"""Temporary staging bridge over the pinned pre-analysis image; no DDL or data rewrite."""

import runpy

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from buddy_api.persistence import database
from buddy_api.persistence.errors import PersistenceUnavailable
from buddy_api.persistence.models import Base

OLD_HEAD = "0003_extractions"
FEATURE_HEAD = "0008_rollback_compatibility"
WORKER_HEAD = "0009_pdf_workers"
ANALYSIS_WORKER_HEAD = "0010_analysis_worker"
VISUAL_QA_HEAD = "0011_visual_qa_pending"


async def check_ready(self):
    """Accept only the explicitly tested schemas, checking every legacy mapped column."""
    try:
        async with self.engine.connect() as connection:
            heads = set(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            )
            if heads not in (
                {OLD_HEAD},
                {FEATURE_HEAD},
                {WORKER_HEAD},
                {ANALYSIS_WORKER_HEAD},
                {VISUAL_QA_HEAD},
            ):
                raise PersistenceUnavailable("Database is outside the tested compatibility window.")
            for table in Base.metadata.sorted_tables:
                await connection.execute(select(table).limit(0))
    except (SQLAlchemyError, OSError, TimeoutError):
        raise PersistenceUnavailable("Database compatibility readiness failed.") from None


def install() -> None:
    # Installed only in the bridge image. The normal application retains its
    # exact-head policy; no environment flag can enable this in feature images.
    database.Database.check_ready = check_ready


if __name__ == "__main__":
    install()
    runpy.run_module("buddy_api", run_name="__main__")
