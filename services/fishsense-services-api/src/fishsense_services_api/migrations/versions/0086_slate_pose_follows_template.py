"""A slate pose serves only on its mission's template (2026-11-09).

Mission 116 was given Tic-Tac-Toe 6 by its species labels, and its automatic
slate-pose fit (2026-10-29) was posed on that template; the owner read a
Tic-Tac-Toe 7 in its frames (2026-11-09), a slate with no template. Clearing
or correcting a mission's template made its pose owed again (0078) but left
the old fit serving: `automatic_measurement_calibrations` took the latest
accepted pose whatever template it was posed on, so 116's 46 automatic
lengths stood on the wrong slate. Now:

* ``automatic_measurement_calibrations``: a slate-pose fit serves (its
  mission's lengths, or as the link's, its borrowers') only while it was
  posed on its mission's current template; otherwise the next in line does
  (0078's order), or nothing. A borrow serves only while its lender's fit
  is current and still on its lender's template.
* lengths measured with the fit stop being current with it
  (`current_automatic_measurements` joins this view), and the fit is not
  deleted: set the template back and it serves again.

The lenders (`automatic_results_store._lenders`) apply the same rule in
Python's SQL. Size-constancy and borrowed rows are untouched. The downgrade
puts 0082's view back. Pinned by tests/test_automatic_slate_pose.py,
tests/test_calibration_borrow.py and tests/test_migration_0086.py.

Revision ID: 0086
Revises: 0085
"""

from alembic import op
from sqlalchemy import text

revision = "0086"
down_revision = "0085"

_PICK = "automatic_measurement_calibrations"
_PICK_0082 = """
    SELECT d.tenant_id, d.id AS mission_id, 'label_free'::text AS calibration_source,
           lf.id AS automatic_laser_calibration_id, NULL::uuid AS laser_calibration_id,
           lf.laser_position, lf.laser_axis, cc.id AS camera_calibration_id,
           cc.camera_matrix
    FROM missions d
    JOIN current_camera_calibrations cc
      ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
     AND cc.camera_model = 'pinhole'
    CROSS JOIN (VALUES (0::smallint), (1::smallint)) aims (aim)
    LEFT JOIN LATERAL (
        SELECT a.id, a.laser_position, a.laser_axis
        FROM current_automatic_laser_calibrations a
        JOIN automatic_laser_calibrations x ON x.tenant_id = a.tenant_id AND x.id = a.id
        WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'
          AND ((a.mission_id = d.id AND x.aim = aims.aim)
               OR (aims.aim = 0 AND a.mission_id = d.calibration_source_mission_id
                   AND x.aim = 0 AND a.method <> 'borrowed'))
          -- A borrow serves while its lender's fit is current (0082).
          AND (a.method <> 'borrowed' OR EXISTS (
              SELECT 1 FROM current_automatic_laser_calibrations l
              WHERE l.tenant_id = a.tenant_id AND l.id = x.borrowed_from))
        ORDER BY (a.method <> 'borrowed') DESC, (a.mission_id = d.id) DESC,
                 (a.method = 'slate_pose') DESC
        LIMIT 1
    ) lf ON true
    WHERE lf.id IS NOT NULL
"""
_PICK_0086 = """
    SELECT d.tenant_id, d.id AS mission_id, 'label_free'::text AS calibration_source,
           lf.id AS automatic_laser_calibration_id, NULL::uuid AS laser_calibration_id,
           lf.laser_position, lf.laser_axis, cc.id AS camera_calibration_id,
           cc.camera_matrix
    FROM missions d
    JOIN current_camera_calibrations cc
      ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
     AND cc.camera_model = 'pinhole'
    CROSS JOIN (VALUES (0::smallint), (1::smallint)) aims (aim)
    LEFT JOIN LATERAL (
        SELECT a.id, a.laser_position, a.laser_axis
        FROM current_automatic_laser_calibrations a
        JOIN automatic_laser_calibrations x ON x.tenant_id = a.tenant_id AND x.id = a.id
        WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'
          AND ((a.mission_id = d.id AND x.aim = aims.aim)
               OR (aims.aim = 0 AND a.mission_id = d.calibration_source_mission_id
                   AND x.aim = 0 AND a.method <> 'borrowed'))
          -- A slate pose serves while it was posed on its mission's
          -- template (0086).
          AND (a.method <> 'slate_pose' OR x.slate_template_id = (
              SELECT o.slate_template_id FROM missions o
              WHERE o.tenant_id = a.tenant_id AND o.id = a.mission_id))
          -- A borrow serves while its lender's fit is current (0082) and on
          -- its lender's template (0086).
          AND (a.method <> 'borrowed' OR EXISTS (
              SELECT 1 FROM current_automatic_laser_calibrations l
              JOIN automatic_laser_calibrations lx
                ON lx.tenant_id = l.tenant_id AND lx.id = l.id
              JOIN missions lm ON lm.tenant_id = l.tenant_id AND lm.id = l.mission_id
              WHERE l.tenant_id = a.tenant_id AND l.id = x.borrowed_from
                AND (l.method <> 'slate_pose'
                     OR lx.slate_template_id = lm.slate_template_id)))
        ORDER BY (a.method <> 'borrowed') DESC, (a.mission_id = d.id) DESC,
                 (a.method = 'slate_pose') DESC
        LIMIT 1
    ) lf ON true
    WHERE lf.id IS NOT NULL
"""


def _view_options(view: str) -> str:
    options = (
        op.get_bind()
        .execute(
            text("SELECT reloptions FROM pg_class WHERE oid = CAST(:v AS regclass)"),
            {"v": view},
        )
        .scalar_one()
    )
    return f" WITH ({', '.join(options)})" if options else ""


def _replace_view(view: str, body: str) -> None:
    """CREATE OR REPLACE drops options it isn't given (security_invoker)."""
    op.execute(f"CREATE OR REPLACE VIEW {view}{_view_options(view)} AS {body}")


def upgrade() -> None:
    _replace_view(_PICK, _PICK_0086)


def downgrade() -> None:
    _replace_view(_PICK, _PICK_0082)
