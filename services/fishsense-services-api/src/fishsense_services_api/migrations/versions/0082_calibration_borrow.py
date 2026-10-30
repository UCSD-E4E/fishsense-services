"""The checked borrow (the owner, 2026-10-29).

An aim with no accepted calibration of its own (nor, for aim 0, its link's)
may take the slate-pose fit of another mission on its device that started
within a day, when that beam runs along the aim's own dot line and reads
the aim's own posed slates at their depth (`calibration_borrow`; the check,
`fishsense_services_processor.automatic_calibration.borrow`). Mission 383's
hogfish, after its 07:01 beam move, is the case: its after-move slate-pose
fit was refused (two posed frames that disagree with the mission), and the
beam of 386, the same FSL03 that day, runs 3-9 px off its after-move line.

* ``method`` may be ``borrowed``; such a row names the lender's row it tried
  (``borrowed_from``), how far that beam runs off the aim's dot line
  (``borrow_line_px``), and the check: the median over slate holds of the
  beam's depth over the pose's, less one (``borrow_depth_ratio``), on
  ``borrow_holds`` holds. An accepted borrow has all four; a refused one
  whatever it got to; any other method none.
* ``automatic_measurement_calibrations``: after the mission's own fit at the
  aim and (aim 0) its link's, an accepted borrow -- while the lender's row
  is still current: a lender refitted, its borrower is owed the borrow again.

The downgrade restores 0081's view and table; it fails while a borrowed row
exists, rather than delete history. Pinned by tests/test_migration_0082.py
and tests/test_calibration_borrow.py.

Revision ID: 0082
Revises: 0081
"""

from alembic import op
from sqlalchemy import text

revision = "0082"
down_revision = "0081"

TABLE = "automatic_laser_calibrations"
_METHOD_CHECK = f"{TABLE}_method_check"
_BORROW_CHECK = f"{TABLE}_borrow_check"
_BORROW_COLUMNS = ("borrowed_from", "borrow_line_px", "borrow_depth_ratio",
                   "borrow_holds")  # fmt: skip

_PICK = "automatic_measurement_calibrations"
_PICK_0081 = """
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
                   AND x.aim = 0))
        ORDER BY (a.mission_id = d.id) DESC, (a.method = 'slate_pose') DESC
        LIMIT 1
    ) lf ON true
    WHERE lf.id IS NOT NULL
"""
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


def _method_check(methods) -> None:
    listed = ", ".join(f"'{m}'" for m in methods)
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP CONSTRAINT {_METHOD_CHECK},
            ADD CONSTRAINT {_METHOD_CHECK} CHECK (method IN ({listed}))
        """)


def upgrade() -> None:
    _method_check(("size_constancy", "slate_pose", "borrowed"))
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN borrowed_from uuid,
            ADD CONSTRAINT {TABLE}_borrowed_from_fkey FOREIGN KEY
                (tenant_id, borrowed_from) REFERENCES {TABLE} (tenant_id, id),
            ADD COLUMN borrow_line_px double precision,
            ADD COLUMN borrow_depth_ratio double precision,
            ADD COLUMN borrow_holds integer,
            ADD CONSTRAINT {_BORROW_CHECK} CHECK (
                CASE WHEN method <> 'borrowed'
                     THEN num_nulls({', '.join(_BORROW_COLUMNS)}) = 4
                     WHEN outcome = 'accepted'
                     THEN num_nonnulls({', '.join(_BORROW_COLUMNS)}) = 4
                     ELSE true END)
        """)
    _replace_view(_PICK, _PICK_0082)


def downgrade() -> None:
    held = (
        op.get_bind()
        .execute(text(f"SELECT count(*) FROM {TABLE} WHERE method = 'borrowed'"))
        .scalar_one()
    )
    if held:
        raise RuntimeError(f"{held} borrowed calibrations: 0081 cannot hold them")
    _replace_view(_PICK, _PICK_0081)
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP CONSTRAINT {_BORROW_CHECK},
            DROP CONSTRAINT {TABLE}_borrowed_from_fkey,
            {", ".join(f"DROP COLUMN {c}" for c in _BORROW_COLUMNS)}
        """)
    _method_check(("size_constancy", "slate_pose"))
