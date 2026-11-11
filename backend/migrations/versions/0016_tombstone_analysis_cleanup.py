"""Permit immutable analysis cleanup only after its deck is tombstoned."""

from alembic import op

revision = "0016_tombstone_analysis_cleanup"
down_revision = "0015_deck_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Analysis rows remain immutable during normal operation. The owning deck's
    # durable tombstone is the sole database-enforced exception for DELETE, so
    # cleanup can remove private data without disabling the trigger or relying
    # on a forgeable session setting.
    op.execute(
        """CREATE OR REPLACE FUNCTION analysis_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          owning_deck UUID;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF TG_TABLE_NAME = 'legacy_analysis_payloads' THEN
              SELECT deck_id INTO owning_deck
                FROM analysis_versions WHERE id = OLD.analysis_id;
            ELSE
              owning_deck := OLD.deck_id;
            END IF;
            IF owning_deck IS NOT NULL AND EXISTS (
              SELECT 1 FROM decks
              WHERE id = owning_deck AND deletion_requested_at IS NOT NULL
            ) THEN
              RETURN OLD;
            END IF;
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
        END $$"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration; restore a verified backup or apply a reviewed repair."
    )
