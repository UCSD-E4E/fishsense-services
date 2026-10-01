"""What a dive is measured with: v1's rule.

Migration 0008's ``effective_laser_calibrations`` said it followed v1's
borrowing, but let a dive's link win over its own calibration, and it had no
plausibility test. v1 (fishsense-lite@a8b2c3bc
dive_controller.get_laser_extrinsics_for_dive and ``_plausible_extrinsics``):

* a dive's **own** calibration wins; only without one does it borrow its link's;
* a stored fit whose baseline -- the norm of laser_position's x and y; both
  producers pad z -- is outside **0.097-0.145 m** counts as no calibration,
  everywhere, borrowed ones included. Eight of v1's 35 stored fits were
  2.35-22.22 cm against a fleet IQR of 9.99-10.45 cm, backing 663 of 3,104
  measurements at -75% to +45% error; dive 518's 2.60 cm fit, borrowed by two
  others, made three dives of wrong lengths.

v2 keeps its refusal rule: a refusal after a fit is the dive's current
calibration, so it has none of its own and falls back to its link.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"

#: fishsense-lite libs/fishsense-shared calibration_bounds.MIN/MAX_BASELINE_M.
MIN_BASELINE_M = 0.097
MAX_BASELINE_M = 0.145


def upgrade() -> None:
    # Unreadable counts as implausible, as v1's `baseline_m` returns inf for it:
    # "cannot tell" and "implausible" want the same answer -- refuse, keep looking.
    op.execute(f"""
        CREATE FUNCTION plausible_laser_baseline(laser_position jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE
                WHEN jsonb_typeof(laser_position) = 'array'
                 AND jsonb_array_length(laser_position) >= 2
                 AND jsonb_typeof(laser_position -> 0) = 'number'
                 AND jsonb_typeof(laser_position -> 1) = 'number'
                THEN sqrt(power((laser_position ->> 0)::double precision, 2)
                        + power((laser_position ->> 1)::double precision, 2))
                     BETWEEN {MIN_BASELINE_M} AND {MAX_BASELINE_M}
                ELSE false
            END
        $$
        """)
    op.execute("""
        CREATE OR REPLACE VIEW effective_laser_calibrations
            WITH (security_invoker = true) AS
            WITH usable AS (
                SELECT * FROM current_laser_calibrations
                WHERE outcome = 'accepted'
                  AND plausible_laser_baseline(laser_position)
            )
            SELECT d.tenant_id,
                   d.id AS dive_id,
                   coalesce(own.id, link.id) AS laser_calibration_id,
                   coalesce(own.dive_id, link.dive_id) AS source_dive_id,
                   own.id IS NULL AS borrowed
            FROM dives d
            LEFT JOIN usable own
              ON own.tenant_id = d.tenant_id AND own.dive_id = d.id
            LEFT JOIN usable link
              ON link.tenant_id = d.tenant_id
             AND link.dive_id = d.calibration_source_dive_id
            WHERE coalesce(own.id, link.id) IS NOT NULL
        """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW effective_laser_calibrations
            WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS dive_id,
                   c.id AS laser_calibration_id,
                   c.dive_id AS source_dive_id,
                   d.calibration_source_dive_id IS NOT NULL AS borrowed
            FROM dives d
            JOIN current_laser_calibrations c
              ON c.tenant_id = d.tenant_id
             AND c.dive_id = coalesce(d.calibration_source_dive_id, d.id)
            WHERE c.outcome = 'accepted'
        """)
    op.execute("DROP FUNCTION plausible_laser_baseline(jsonb)")
