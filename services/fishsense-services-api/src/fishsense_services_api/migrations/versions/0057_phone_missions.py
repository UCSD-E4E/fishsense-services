"""Phone missions (Deep water phase 7; the owner, 2026-10-21).

FishSense Mobile captures are grouped into missions as they sync: the same
phone, at most 2 hours and 1 km from the capture before. A Lite mission is a
folder of photos. A phone mission has none, so ``missions.source`` says which
kind a mission is. A Lite mission keeps its ``source_path``, and a phone
mission has none (the CHECK). Every mission before 0057 is a Lite one, the
default.

The column is what keeps phone missions out of the folder-reading code (port
and purpose backfill, deployment inference, the public map): they filter on
``source = 'lite'``.

Revision ID: 0057
Revises: 0056
"""

from alembic import op

revision = "0057"
down_revision = "0056"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE missions
            ADD COLUMN source text NOT NULL DEFAULT 'lite'
                CHECK (source IN ('lite', 'mobile')),
            ALTER COLUMN source_path DROP NOT NULL,
            ADD CONSTRAINT missions_folder_is_lites
                CHECK ((source = 'lite') = (source_path IS NOT NULL))
        """)
    # Grouping looks up a phone's captures near a time.
    op.execute(
        "CREATE INDEX captures_device_time_idx "
        "ON captures (tenant_id, device_id, captured_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX captures_device_time_idx")
    # A phone mission has no folder to go back to: refuse rather than invent one.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM missions WHERE source = 'mobile') THEN
                RAISE EXCEPTION 'phone missions exist; 0057 cannot be undone';
            END IF;
        END $$
        """)
    op.execute("""
        ALTER TABLE missions
            DROP CONSTRAINT missions_folder_is_lites,
            ALTER COLUMN source_path SET NOT NULL,
            DROP COLUMN source
        """)
