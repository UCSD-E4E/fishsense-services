"""A calibration's error bar (the owner, 2026-10-26).

Of 32 missions with accepted human calibrations the automatic fits refused
24. The owner: a weak fit (few frames, few posed slates) is less accurate,
not wrong, so it should answer with its error rather than refuse, and every
length should be shown with it. The slate-pose fit now answers from two
posed slates and records its length scale's SD:

* ``automatic_laser_calibrations.scale_sd``: the SD of the lengths' scale
  on this calibration (0.02 = 2%), positive, and only on an accepted one.
  None on rows from before, and on size-constancy fits, which don't record
  one: no bar is not a zero bar.

A length's calibration error is its length times its calibration's
``scale_sd``, read by joining the calibration by id; no view changes (the
current views carry their columns fixed, and the downgrade could not take
one back out).

The downgrade fails while any calibration carries an error bar, rather than
drop it.

Revision ID: 0079
Revises: 0078
"""

from alembic import op
from sqlalchemy import text

revision = "0079"
down_revision = "0078"

TABLE = "automatic_laser_calibrations"
_CHECK = f"{TABLE}_scale_sd_check"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN scale_sd double precision,
            ADD CONSTRAINT {_CHECK}
                CHECK (scale_sd IS NULL OR (scale_sd > 0 AND outcome = 'accepted'))
        """)


def downgrade() -> None:
    carried = (
        op.get_bind()
        .execute(text(f"SELECT count(*) FROM {TABLE} WHERE scale_sd IS NOT NULL"))
        .scalar_one()
    )
    if carried:
        raise RuntimeError(f"{carried} calibrations carry an error bar: 0078 cannot")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN scale_sd")
