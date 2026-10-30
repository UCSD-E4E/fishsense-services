"""The unchecked borrow (the owner, 2026-10-30).

0082 accepted a borrow only once the aim's own posed slates checked it; an aim
with no slates to pose (no template, no slate frame, too few posed points) was
refused, 33 of the 88 tried on 2026-10-30 -- though the lender's beam ran
along each one's dot line. The owner: borrow them with a wider error bar
(`calibration_borrow.LINE_ONLY_SD_PER_M`). An accepted borrow still names
its lender and line offset, but may lack the check: ``borrow_depth_ratio``
and ``borrow_holds`` are both set (checked) or both null (the line alone).

The downgrade restores 0082's check; it fails while an unchecked borrow
exists, rather than delete history. Pinned by tests/test_migration_0083.py
and tests/test_calibration_borrow.py.

Revision ID: 0083
Revises: 0082
"""

from alembic import op
from sqlalchemy import text

revision = "0083"
down_revision = "0082"

TABLE = "automatic_laser_calibrations"
_BORROW_CHECK = f"{TABLE}_borrow_check"
_BORROW_COLUMNS = ("borrowed_from", "borrow_line_px", "borrow_depth_ratio",
                   "borrow_holds")  # fmt: skip
_ALL = ", ".join(_BORROW_COLUMNS)

_CHECK_0082 = f"""
    CASE WHEN method <> 'borrowed' THEN num_nulls({_ALL}) = 4
         WHEN outcome = 'accepted' THEN num_nonnulls({_ALL}) = 4
         ELSE true END
"""
_CHECK_0083 = f"""
    CASE WHEN method <> 'borrowed' THEN num_nulls({_ALL}) = 4
         WHEN outcome = 'accepted'
         THEN num_nonnulls(borrowed_from, borrow_line_px) = 2
              AND num_nulls(borrow_depth_ratio, borrow_holds) IN (0, 2)
         ELSE true END
"""


def _replace_check(definition: str) -> None:
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP CONSTRAINT {_BORROW_CHECK},
            ADD CONSTRAINT {_BORROW_CHECK} CHECK ({definition})
        """)


def upgrade() -> None:
    _replace_check(_CHECK_0083)


def downgrade() -> None:
    held = op.get_bind().execute(text(f"""
            SELECT count(*) FROM {TABLE}
            WHERE method = 'borrowed' AND outcome = 'accepted'
              AND borrow_depth_ratio IS NULL
            """)).scalar_one()
    if held:
        raise RuntimeError(f"{held} unchecked borrows: 0082 cannot hold them")
    _replace_check(_CHECK_0082)
