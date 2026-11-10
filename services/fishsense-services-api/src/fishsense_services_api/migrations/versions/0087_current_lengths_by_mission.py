"""Current lengths a mission at a time (2026-11-09).

On production every mission's automatic results page returned a 500: its
read of ``current_automatic_measurements`` for one mission ran past the app
role's 30 s statement timeout (the API's log: ``QueryCanceledError``,
``automatic_review_store.mission_automatic``), as the Today map's automatic
lengths had that morning. Two causes, both measured on a replica of
production's rows (70,378 lengths, 1,725 laser calibrations, 541 missions):

* The view keeps one row per frame (``DISTINCT ON (tenant_id,
  capture_id)``), and Postgres pushes a filter into it only on those keys.
  A mission's read therefore built the whole tenant's lengths first.
* Building them asked ``automatic_measurement_calibrations`` for each length
  row, and each ask sorted every laser calibration twice: once for
  ``current_automatic_laser_calibrations`` and again for a borrow's lender
  (0082). That is about 3 ms a row.

The fixes:

* ``automatic_laser_calibrations_current_idx`` on (tenant, mission, method,
  aim, seq). The pick now reads "current" as *no newer row of its mission,
  method and aim*: the same rows as the ``DISTINCT ON`` view, with an index
  lookup each instead of a sort. 0086's rule is kept: a slate pose serves
  while it was posed on its mission's template, and a borrow while its
  lender's fit is current and (a slate pose) on its lender's template. Its
  columns are unchanged.
* ``current_automatic_measurements`` asks the pick once per length, for that
  frame's own mission, and keeps the pick whose calibration is at the frame's
  aim (0081's rule), rather than joining the pick whole. Its ``DISTINCT ON``
  adds the frame's mission, which a frame has exactly one of, so it picks the
  same row; a mission's read now stays inside the mission. It gains
  ``mission_id``, at the end.

On the replica, before and after: one frame 2.5 ms and 2.1 ms; one mission
over 60 s and 50 ms (2.3 s when filtered through ``captures`` rather than
``mission_id``); the lab tenant over 60 s and 2.4 s; every tenant 41 s and
2.4 s. Against 0086's views on the same rows (1,748 laser calibrations), both
gave the same: 27,313 of 27,313 current lengths, and 212 of 212 picks. No
accepted slate pose there was off its mission's template, so 0086's rule is
pinned by its tests (tests/test_automatic_slate_pose.py,
tests/test_calibration_borrow.py), not by the replica.

The downgrade puts 0086's definitions back (0086's pick, 0081's lengths) and
drops the index. A view's column cannot be dropped by CREATE OR REPLACE, so
the lengths view is rebuilt without ``mission_id``, and the research export
over it with it, each from the definition, options and readers Postgres
holds (as 0074's downgrade does).

Pinned by tests/test_migration_0087.py.

Revision ID: 0087
Revises: 0086
"""

from alembic import op
from sqlalchemy import text

revision = "0087"
down_revision = "0086"

_INDEX = "automatic_laser_calibrations_current_idx"

_PICK = "automatic_measurement_calibrations"
_LENGTHS = "current_automatic_measurements"
_EXPORT = "automatic_results_export"


def _current(alias: str) -> str:
    """`alias` is the newest row of its mission, method and aim: the row
    ``current_automatic_laser_calibrations`` keeps, by an index lookup."""
    return f"""NOT EXISTS (
        SELECT 1 FROM automatic_laser_calibrations n
        WHERE n.tenant_id = {alias}.tenant_id AND n.mission_id = {alias}.mission_id
          AND n.method = {alias}.method AND n.aim = {alias}.aim
          AND n.seq > {alias}.seq)"""


_PICK_0086 = f"""
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
        FROM automatic_laser_calibrations a
        WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'
          AND {_current("a")}
          AND ((a.mission_id = d.id AND a.aim = aims.aim)
               OR (aims.aim = 0 AND a.mission_id = d.calibration_source_mission_id
                   AND a.aim = 0 AND a.method <> 'borrowed'))
          -- A slate pose serves while it was posed on its mission's
          -- template (0086).
          AND (a.method <> 'slate_pose' OR a.slate_template_id = (
              SELECT o.slate_template_id FROM missions o
              WHERE o.tenant_id = a.tenant_id AND o.id = a.mission_id))
          -- A borrow serves while its lender's fit is current (0082) and on
          -- its lender's template (0086).
          AND (a.method <> 'borrowed' OR EXISTS (
              SELECT 1 FROM automatic_laser_calibrations l
              JOIN missions lm ON lm.tenant_id = l.tenant_id AND lm.id = l.mission_id
              WHERE l.tenant_id = a.tenant_id AND l.id = a.borrowed_from
                AND {_current("l")}
                AND (l.method <> 'slate_pose'
                     OR l.slate_template_id = lm.slate_template_id)))
        ORDER BY (a.method <> 'borrowed') DESC, (a.mission_id = d.id) DESC,
                 (a.method = 'slate_pose') DESC
        LIMIT 1
    ) lf ON true
    WHERE lf.id IS NOT NULL
"""

_COLUMNS = """
    m.id, m.tenant_id, m.seq, m.producer, m.capture_id,
    m.automatic_head_tail_prediction_id, m.calibration_source,
    m.automatic_laser_calibration_id, m.laser_calibration_id,
    m.camera_calibration_id, m.length_m, m.depth_m, m.refusal, m.algorithm,
    m.algorithm_version, m.core_version, m.created_at,
    m.automatic_keypoint_prediction_id
"""

_HEAD_TAIL = """
    JOIN LATERAL (
        SELECT h.id FROM automatic_head_tail_predictions h
        WHERE h.tenant_id = m.tenant_id AND h.capture_id = m.capture_id
        ORDER BY h.seq DESC
        LIMIT 1
    ) ht ON ht.id = m.automatic_head_tail_prediction_id
"""

_LENGTHS_0086 = f"""
    SELECT DISTINCT ON (m.tenant_id, c.mission_id, m.capture_id) {_COLUMNS},
           c.mission_id
    FROM automatic_measurements m
    JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
    {_HEAD_TAIL}
    -- The frame's mission's pick whose calibration is at the frame's aim
    -- (0081): asked per length, so a mission's read asks only for its own.
    JOIN LATERAL (
        SELECT mc.calibration_source, mc.automatic_laser_calibration_id,
               mc.laser_calibration_id
        FROM automatic_measurement_calibrations mc
        JOIN automatic_laser_calibrations x
          ON x.tenant_id = mc.tenant_id AND x.id = mc.automatic_laser_calibration_id
        WHERE mc.tenant_id = c.tenant_id AND mc.mission_id = c.mission_id
          AND x.aim = beam_aim(c.tenant_id, c.mission_id, c.captured_at)
        LIMIT 1
    ) mc ON mc.calibration_source = m.calibration_source
        AND NOT mc.automatic_laser_calibration_id
                IS DISTINCT FROM m.automatic_laser_calibration_id
        AND NOT mc.laser_calibration_id IS DISTINCT FROM m.laser_calibration_id
    ORDER BY m.tenant_id, c.mission_id, m.capture_id, m.seq DESC
"""

# -- 0086's definitions, for the downgrade: 0086's pick and 0081's lengths.

_PICK_PREVIOUS = """
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

_LENGTHS_PREVIOUS = f"""
    SELECT DISTINCT ON (m.tenant_id, m.capture_id) {_COLUMNS}
    FROM automatic_measurements m
    JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
    {_HEAD_TAIL}
    JOIN automatic_measurement_calibrations mc
      ON mc.tenant_id = c.tenant_id AND mc.mission_id = c.mission_id
     AND mc.calibration_source = m.calibration_source
     AND NOT mc.automatic_laser_calibration_id
             IS DISTINCT FROM m.automatic_laser_calibration_id
     AND NOT mc.laser_calibration_id IS DISTINCT FROM m.laser_calibration_id
     -- Its calibration's aim is its frame's (0081).
     AND beam_aim(c.tenant_id, c.mission_id, c.captured_at) = (
         SELECT x.aim FROM automatic_laser_calibrations x
         WHERE x.tenant_id = mc.tenant_id AND x.id = mc.automatic_laser_calibration_id)
    ORDER BY m.tenant_id, m.capture_id, m.seq DESC
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
    """CREATE OR REPLACE drops options it isn't given (security_invoker); it
    keeps grants and dependent views, and may only add columns at the end."""
    op.execute(f"CREATE OR REPLACE VIEW {view}{_view_options(view)} AS {body}")


def _held(view: str):
    """A view's definition, options and SELECT grants, as Postgres holds them."""
    row = (
        op.get_bind()
        .execute(
            text("""
                SELECT pg_get_viewdef(c.oid, true) AS definition, c.reloptions,
                       ARRAY(SELECT DISTINCT a.grantee::regrole::text
                             FROM aclexplode(c.relacl) a
                             WHERE a.privilege_type = 'SELECT'
                               AND a.grantee <> c.relowner) AS readers
                FROM pg_class c WHERE c.oid = CAST(:v AS regclass)
                """),
            {"v": view},
        )
        .one()
    )
    return row.definition, row.reloptions, row.readers


def _dependents(view: str) -> list[str]:
    """The views built directly on `view`."""
    return sorted(
        op.get_bind()
        .execute(
            text("""
                SELECT DISTINCT v.oid::regclass::text
                FROM pg_depend d
                JOIN pg_rewrite r ON r.oid = d.objid
                JOIN pg_class v ON v.oid = r.ev_class
                WHERE d.refobjid = CAST(:v AS regclass) AND v.oid <> d.refobjid
                """),
            {"v": view},
        )
        .scalars()
    )


def _create(view: str, definition: str, options, readers) -> None:
    with_ = f" WITH ({', '.join(options)})" if options else ""
    op.execute(f"CREATE VIEW {view}{with_} AS {definition.rstrip(';')}")
    for reader in readers:
        op.execute(f"GRANT SELECT ON {view} TO {reader}")


def upgrade() -> None:
    op.execute(f"""
        CREATE INDEX {_INDEX} ON automatic_laser_calibrations
            (tenant_id, mission_id, method, aim, seq)
        """)
    _replace_view(_PICK, _PICK_0086)
    _replace_view(_LENGTHS, _LENGTHS_0086)


def downgrade() -> None:
    # Only the export is built on the lengths; anything else would be lost.
    found = _dependents(_LENGTHS)
    if found != [_EXPORT]:
        raise RuntimeError(f"{_LENGTHS}: views built on it: {found}")
    if _dependents(_EXPORT):
        raise RuntimeError(f"{_EXPORT}: views built on it: {_dependents(_EXPORT)}")
    lengths = _held(_LENGTHS)
    export = _held(_EXPORT)
    op.execute(f"DROP VIEW {_EXPORT}")
    op.execute(f"DROP VIEW {_LENGTHS}")
    _create(_LENGTHS, _LENGTHS_PREVIOUS, *lengths[1:])
    _create(_EXPORT, *export)
    _replace_view(_PICK, _PICK_PREVIOUS)
    op.execute(f"DROP INDEX {_INDEX}")
