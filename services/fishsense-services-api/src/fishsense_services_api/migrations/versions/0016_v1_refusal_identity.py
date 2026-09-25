"""Give migrated v1 refusals an identity on laser_calibrations.

v1 kept a dive's calibration refusal as columns on ``dive``, not as a row, so
a migrated refusal has no ``v1_id`` of its own. ``v1_refusal_dive_id`` (the v1
dive it came from) makes the migration idempotent for refusals and marks the
row as migrated -- which, like ``v1_id``, is what lets its producer stay
unknown.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE laser_calibrations
            ADD COLUMN v1_refusal_dive_id bigint UNIQUE,
            DROP CONSTRAINT laser_calibrations_producer_known_check,
            ADD CONSTRAINT laser_calibrations_producer_known_check CHECK (
                producer IS NOT NULL
                OR v1_id IS NOT NULL
                OR v1_refusal_dive_id IS NOT NULL
            )
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE laser_calibrations
            DROP CONSTRAINT laser_calibrations_producer_known_check,
            ADD CONSTRAINT laser_calibrations_producer_known_check
                CHECK (producer IS NOT NULL OR v1_id IS NOT NULL),
            DROP COLUMN v1_refusal_dive_id
        """)
