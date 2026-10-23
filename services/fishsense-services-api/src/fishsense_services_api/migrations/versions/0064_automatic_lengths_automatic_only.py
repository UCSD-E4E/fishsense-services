"""Automatic lengths rest on the automatic stack alone (owner, 2026-10-22).

0036's `automatic_measurement_calibrations` gave a mission's automatic lengths
its own accepted label-free fit, else its calibration link's, else
production's stored calibration -- the human path's, fitted from human slate
labels. That last fallback put a human-derived beam under an automatic length
(mission 471: 4 detector slate frames, its label-free fit refused, measured
on the stored slate calibration). Now there is no fallback to it: a mission
with no accepted label-free fit of its own or through its link has no
automatic lengths.

The link stays: it is production's pairing of a fish dive with its
calibration session, and the fit it points at is label-free.

Columns are unchanged (`calibration_source` is always ``label_free`` now;
`laser_calibration_id` always null), so the one dependent view,
`current_automatic_measurements`, is unchanged: lengths measured on a stored
calibration (1,457 in 31 missions at the change) stay in
`automatic_measurements` as history and leave the current results and the
export.

Revision ID: 0064
Revises: 0063
"""

from alembic import op

revision = "0064"
down_revision = "0063"

_LABEL_FREE = """
    LEFT JOIN LATERAL (
        SELECT a.id, a.laser_position, a.laser_axis
        FROM current_automatic_laser_calibrations a
        WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'
          AND a.mission_id = ANY (ARRAY[d.id, d.calibration_source_mission_id])
        ORDER BY (a.mission_id = d.id) DESC
        LIMIT 1
    ) lf ON true
"""


def upgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE VIEW automatic_measurement_calibrations
            WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS mission_id,
                   'label_free'::text AS calibration_source,
                   lf.id AS automatic_laser_calibration_id,
                   NULL::uuid AS laser_calibration_id,
                   lf.laser_position,
                   lf.laser_axis,
                   cc.id AS camera_calibration_id,
                   cc.camera_matrix
            FROM missions d
            JOIN current_camera_calibrations cc
              ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
             AND cc.camera_model = 'pinhole'
            {_LABEL_FREE}
            WHERE lf.id IS NOT NULL
        """)


def downgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE VIEW automatic_measurement_calibrations
            WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS mission_id,
                   CASE WHEN lf.id IS NOT NULL THEN 'label_free' ELSE 'stored' END
                       AS calibration_source,
                   lf.id AS automatic_laser_calibration_id,
                   CASE WHEN lf.id IS NULL THEN g.laser_calibration_id END
                       AS laser_calibration_id,
                   coalesce(lf.laser_position, g.laser_position) AS laser_position,
                   coalesce(lf.laser_axis, g.laser_axis) AS laser_axis,
                   cc.id AS camera_calibration_id,
                   cc.camera_matrix
            FROM missions d
            JOIN current_camera_calibrations cc
              ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
             AND cc.camera_model = 'pinhole'
            {_LABEL_FREE}
            LEFT JOIN mission_laser_geometry g
              ON g.tenant_id = d.tenant_id AND g.mission_id = d.id
            WHERE lf.id IS NOT NULL OR g.laser_calibration_id IS NOT NULL
        """)
