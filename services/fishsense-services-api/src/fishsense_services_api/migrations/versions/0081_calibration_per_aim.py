"""A calibration per laser aim (the owner, 2026-10-29).

0073's partial run calibrated and measured a mission only before a recorded
beam move and refused the rest: 37 missions, ~4,300 frames after their move.
Each aim is now its own calibration. Mission 383's hogfish (07:14, after its
07:01 move) had no length although its human calibration, fitted on the
after-move slates, shows the second aim is calibratable.

* ``beam_aim(tenant, mission, captured_at)``: a frame's aim -- 0 with no
  current move or captured no later than its ``moved_after``, 1 from its
  ``moved_before`` on, NULL inside the window (neither aim's).
  ``before_beam_move`` is ``beam_aim = 0`` and stays, for the human path.
* ``automatic_laser_calibrations.aim`` (0 for every row so far: each was
  fitted before its move, or on a mission with none);
  ``current_automatic_laser_calibrations`` is the latest per mission, method
  and aim. Same columns: a reader takes the aim off the row by its key, as
  0078 did the template.
* ``automatic_measurement_calibrations``: one row per mission and aim. Of
  the accepted calibrations: its own fit at that aim (the slate pose, then
  size constancy), then -- aim 0 only -- its link's at aim 0. Same
  columns. An aim with neither stays uncalibrated: borrowing a fit from
  another mission on the same device is held back until it can be checked
  against the borrower's own slates (2026-10-29: along the dot line alone,
  5 of 33 own-fit controls read 8-19% off at 2 m).
* ``current_automatic_measurements``: a length is current only when its
  calibration's aim is its frame's. It now reads the beam moves, so the
  research role reads the lab's (``mission_beam_moves``, as 0076 did):
  ``automatic_results_export`` is built on it.

The downgrade restores 0080's views and table; it fails while a row of the
second aim exists, rather than delete history. Pinned by
tests/test_migration_0081.py and tests/test_calibration_per_aim.py.

Revision ID: 0081
Revises: 0080
"""

from alembic import op
from sqlalchemy import text

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0081"
down_revision = "0080"

TABLE = "automatic_laser_calibrations"
_AIM_CHECK = f"{TABLE}_aim_check"

_MOVES = "mission_beam_moves"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"

_CURRENT = "current_automatic_laser_calibrations"
_CURRENT_EDITS = (
    ("SELECT DISTINCT ON (tenant_id, mission_id, method) id,",
     "SELECT DISTINCT ON (tenant_id, mission_id, method, aim) id,"),
    ("ORDER BY tenant_id, mission_id, method, seq DESC",
     "ORDER BY tenant_id, mission_id, method, aim, seq DESC"),
)  # fmt: skip

_PICK = "automatic_measurement_calibrations"
_PICK_0080 = """
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
     JOIN current_camera_calibrations cc ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id AND cc.camera_model = 'pinhole'::text
     LEFT JOIN LATERAL ( SELECT a.id,
            a.laser_position,
            a.laser_axis
           FROM current_automatic_laser_calibrations a
          WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'::text AND (a.mission_id = ANY (ARRAY[d.id, d.calibration_source_mission_id]))
          ORDER BY (a.mission_id = d.id) DESC, (a.method = 'slate_pose'::text) DESC
         LIMIT 1) lf ON true
  WHERE lf.id IS NOT NULL
"""
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

_LENGTHS = "current_automatic_measurements"
_LENGTHS_0080 = """
 SELECT DISTINCT ON (m.tenant_id, m.capture_id) m.id,
    m.tenant_id,
    m.seq,
    m.producer,
    m.capture_id,
    m.automatic_head_tail_prediction_id,
    m.calibration_source,
    m.automatic_laser_calibration_id,
    m.laser_calibration_id,
    m.camera_calibration_id,
    m.length_m,
    m.depth_m,
    m.refusal,
    m.algorithm,
    m.algorithm_version,
    m.core_version,
    m.created_at,
    m.automatic_keypoint_prediction_id
   FROM automatic_measurements m
     JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
     JOIN LATERAL ( SELECT h.id
           FROM automatic_head_tail_predictions h
          WHERE h.tenant_id = m.tenant_id AND h.capture_id = m.capture_id
          ORDER BY h.seq DESC
         LIMIT 1) ht ON ht.id = m.automatic_head_tail_prediction_id
     JOIN automatic_measurement_calibrations mc ON mc.tenant_id = c.tenant_id AND mc.mission_id = c.mission_id AND mc.calibration_source = m.calibration_source AND NOT mc.automatic_laser_calibration_id IS DISTINCT FROM m.automatic_laser_calibration_id AND NOT mc.laser_calibration_id IS DISTINCT FROM m.laser_calibration_id
  ORDER BY m.tenant_id, m.capture_id, m.seq DESC
"""
_SAME_AIM = """
       -- Its calibration's aim is its frame's (0081).
       AND beam_aim(c.tenant_id, c.mission_id, c.captured_at) = (
           SELECT x.aim FROM automatic_laser_calibrations x
           WHERE x.tenant_id = mc.tenant_id AND x.id = mc.automatic_laser_calibration_id)
  ORDER BY m.tenant_id, m.capture_id, m.seq DESC
"""
_LENGTHS_0081 = _LENGTHS_0080.replace(
    "\n  ORDER BY m.tenant_id, m.capture_id, m.seq DESC\n", _SAME_AIM
)


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


def _edit_view(view: str, edits) -> None:
    body = (
        op.get_bind()
        .execute(text("SELECT pg_get_viewdef(CAST(:v AS regclass), true)"), {"v": view})
        .scalar_one()
    )
    for old, new in edits:
        if body.count(old) != 1:
            raise RuntimeError(f"{view}: expected one {old!r} in its definition")
        body = body.replace(old, new)
    _replace_view(view, body.rstrip(";"))


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN aim smallint NOT NULL DEFAULT 0,
            ADD CONSTRAINT {_AIM_CHECK} CHECK (aim IN (0, 1))
        """)
    op.execute("""
        CREATE FUNCTION beam_aim(p_tenant uuid, p_mission uuid, p_at timestamptz)
        RETURNS smallint LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT (CASE
                WHEN b.moved_after IS NULL OR p_at IS NULL OR p_at <= b.moved_after
                    THEN 0
                WHEN p_at >= b.moved_before THEN 1
            END)::smallint
            FROM (SELECT 1) one
            LEFT JOIN public.current_mission_beam_moves b
              ON b.tenant_id = p_tenant AND b.mission_id = p_mission
        $$
        """)
    _edit_view(_CURRENT, _CURRENT_EDITS)
    _replace_view(_PICK, _PICK_0081)
    _replace_view(_LENGTHS, _LENGTHS_0081)
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{_MOVES} FOR SELECT "
        f"TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{_MOVES} AS RESTRICTIVE "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(f"GRANT SELECT ON {_MOVES}, current_{_MOVES} TO {RESEARCH_ROLE}")


def downgrade() -> None:
    held = (
        op.get_bind()
        .execute(text(f"SELECT count(*) FROM {TABLE} WHERE aim <> 0"))
        .scalar_one()
    )
    if held:
        raise RuntimeError(
            f"{held} calibrations of a second aim: 0080 cannot hold them"
        )
    op.execute(f"REVOKE SELECT ON {_MOVES}, current_{_MOVES} FROM {RESEARCH_ROLE}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{_MOVES}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{_MOVES}")
    _replace_view(_LENGTHS, _LENGTHS_0080)
    _replace_view(_PICK, _PICK_0080)
    _edit_view(_CURRENT, [(new, old) for old, new in _CURRENT_EDITS])
    op.execute("DROP FUNCTION beam_aim(uuid, uuid, timestamptz)")
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP CONSTRAINT {_AIM_CHECK},
            DROP COLUMN aim
        """)
