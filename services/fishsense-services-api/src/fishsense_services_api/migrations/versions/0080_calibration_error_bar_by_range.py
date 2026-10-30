"""A calibration's error bar at each range (2026-10-27).

0079 gave each slate-pose calibration one SD for its lengths' scale, the
median of the depth error over 0.5-5 m. Mission 279's beam was right at
2.5 m, +63% at 0.75 m and -26% at 4 m: one summary read it as -6.4% +-22%,
and its fish, at 0.75-1.9 m, came out 14-63% long. The slate-pose fit now
casts its beam from the housing's laser and gives its bar at each range
(slate-pose-6, contract v26):

* ``automatic_laser_calibrations.scale_sd_by_range``: ``[[range m, SD],
  ...]``, a JSON array, and only on an accepted calibration (with its
  ``scale_sd``, the widest of it over the fish's ranges). None on rows from
  before and on size-constancy fits.
* ``calibration_sd_at(by_range, fallback, depth_m)``: the bar at a length's
  depth -- linear between the ranges given, the nearest one's beyond them --
  or `fallback` (the calibration's ``scale_sd``) where it gives none or the
  length has no depth. A length's calibration error is its length times
  this; no view changes.

The downgrade fails while any calibration carries a bar by range, rather
than drop it.

Revision ID: 0080
Revises: 0079
"""

from alembic import op
from sqlalchemy import text

revision = "0080"
down_revision = "0079"

TABLE = "automatic_laser_calibrations"
_CHECK = f"{TABLE}_scale_sd_by_range_check"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN scale_sd_by_range jsonb,
            ADD CONSTRAINT {_CHECK}
                CHECK (scale_sd_by_range IS NULL
                       OR (jsonb_typeof(scale_sd_by_range) = 'array'
                           AND outcome = 'accepted' AND scale_sd IS NOT NULL))
        """)
    op.execute("""
        CREATE FUNCTION calibration_sd_at(
            by_range jsonb, fallback double precision, depth_m double precision
        ) RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
            WITH at AS (
                SELECT (e->>0)::double precision AS z, (e->>1)::double precision AS sd
                FROM jsonb_array_elements(by_range) e
            ),
            lo AS (SELECT z, sd FROM at WHERE z <= depth_m ORDER BY z DESC LIMIT 1),
            hi AS (SELECT z, sd FROM at WHERE z >= depth_m ORDER BY z LIMIT 1)
            SELECT CASE
                WHEN by_range IS NULL OR depth_m IS NULL THEN fallback
                ELSE coalesce(
                    (SELECT CASE WHEN hi.z = lo.z THEN lo.sd
                                 ELSE lo.sd + (hi.sd - lo.sd)
                                      * (depth_m - lo.z) / (hi.z - lo.z) END
                     FROM lo, hi),
                    (SELECT sd FROM lo), (SELECT sd FROM hi), fallback)
            END
        $$
        """)


def downgrade() -> None:
    carried = (
        op.get_bind()
        .execute(
            text(f"SELECT count(*) FROM {TABLE} WHERE scale_sd_by_range IS NOT NULL")
        )
        .scalar_one()
    )
    if carried:
        raise RuntimeError(
            f"{carried} calibrations carry an error bar by range: 0079 cannot"
        )
    op.execute(
        "DROP FUNCTION calibration_sd_at(jsonb, double precision, double precision)"
    )
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN scale_sd_by_range")
