"""`dive_pipeline_status`: one row per dive, v1's shape, v2's cohorts.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
views.py:115-417 (`DIVE_PIPELINE_STATUS_VIEW_SQL`), which Superset's pipeline
datasets read (deploy/superset/datasets/). v1's shape is kept -- its column
names, order and value casing -- because the imported datasets filter
`priority = 'HIGH'` and fail silently on anything else:

* ``dive_id`` is the dive's ``number`` (v1's id for a migrated dive, 0019),
  ``priority`` is upper-cased (v1's enum names), ``dive_slate_id`` is the
  template's number;
* v1's rules hold: one row per dive (no WHERE: a duplicate dive has a row);
  "complete" is never vacuous (zero rows of a kind reads false, and every
  ``*_preprocessed`` flag needs a qualifying capture); every capture
  correlation is gated on ``is_canonical``.

**Each stage column is v2's cohort's, not a restatement of v1's.** The stores
name each selector's predicate (``*_COHORT``: every term but the tenant and
the priority) and, where a stage has a "done" flag, the work it is done with
(``*_WORK``). The view reads those strings:

* a ``*_pending`` column per selector -- 0.1 laser preprocess, laser
  prediction, 1 clustering, 2 species, 5.1 head/tail preprocess and
  prediction, 9 slate, 13 slate calibration and checkerboard, laser depth,
  14 measure -- is ``priority = 'high' AND <cohort>``: exactly the dives the
  selector would pick (tests/test_dive_pipeline_status_view.py iterates each
  selector over a seeded corpus and compares);
* v1's ``*_preprocessed`` flags are "a qualifying capture exists and the
  stage has no work", with the stage's own work predicate. Where v2's cohort
  differs from v1's, the column follows v2: a laser sentinel is not
  preprocessed, and a live label flagged ``needs_reprocess`` is a redraw
  owed (0.1, 2, 5.1, 9); a superseded head/tail row, species marker or slate
  label does not count (5.1, 9); a completed species sentinel is done (2);
* the cohorts' v2-only terms -- a pinhole camera (``camera_sql``) for 0.1,
  prediction, 2 and 5.1; a stageable template for 9; a camera and a
  scalable template for 13 -- are in the ``*_pending`` columns and not in the
  "done" flags, so a dive the pipeline cannot run reads not-done and
  not-pending: blocked, not queued;
* ``calibrated`` / ``calibration_source`` read ``effective_laser_calibrations``
  (0018): own and plausible, else the link's. v1 read any extrinsics row, so
  a refused or implausible fit read calibrated there and not here;
* ``measured`` is "a current server measurement (§9.13) on a canonical
  capture, and no ``measurement_work`` (0026)": a frame counts as measured
  only under the calibration the dive resolves to today (v1's rule), a stale
  binding on a high-priority dive is not current (0028), and a frame whose
  very inputs were refused (0025) is not work.

The v1 labeling-complete flags have no v2 cohort to mirror (the laser
validation cohort, like v1's, is not canonical-gated) and keep v1's view
definition: at least one live completed row on a canonical capture, and no
live incomplete one.

**What it costs.** On the production rehearsal (525 dives, 134k captures) a
first cut took 35 s a read, and Superset reads it five times per chart. So:

* ``captures (tenant_id, dive_id)`` is indexed: every column, and every
  cohort, asks for a dive's canonical captures, and each did it by scanning
  the table;
* ``laser_depth_work`` (0026) looks a capture's current depth up per capture,
  and ``current_laser_depths`` leads its ``DISTINCT ON`` with the tenant so
  the lookup reaches a new index -- 0028's fix for ``current_measurements``,
  applied to depths: its anti join rescanned every current depth per capture
  (16 s). Its rows are unchanged;
* the rows come from ``dive_pipeline_status_rows()``, a SQL function that
  runs as its caller with ``jit = off``: the plan's estimated cost is far
  past ``jit_above_cost``, and Postgres spent 12-17 s compiling it for a 2 s
  read. A view cannot carry a setting; the view is a plain select from it.

**Frozen at migration time.** A function stores the SQL it was created with,
so this one holds the stores' predicates as they are today, and the predictor
versions spelled below. A change to any of them must ship a migration that
recreates the function; `RENDERED_SHA256` and the version constants are
pinned by tests so the change cannot be made silently.

Revision ID: 0029
Revises: 0028
"""

from alembic import context, op

from fishsense_services_api.clustering_store import CLUSTERING_COHORT, VALID_LASER
from fishsense_services_api.headtail_store import (
    HEADTAIL_PREPROCESS_COHORT,
    HEADTAIL_PREPROCESS_WORK,
    headtail_prediction_cohort,
)
from fishsense_services_api.laser_calibration_store import (
    CHECKERBOARD_CALIBRATION_COHORT,
    LASER_CALIBRATION_COHORT,
)
from fishsense_services_api.laser_depth_store import LASER_DEPTH_COHORT
from fishsense_services_api.laser_store import (
    LASER_PREPROCESS_COHORT,
    LASER_PREPROCESS_WORK,
    laser_prediction_cohort,
)
from fishsense_services_api.measurement_store import MEASUREMENT_COHORT
from fishsense_services_api.slate_store import (
    slate_preprocess_cohort,
    slate_preprocess_work,
)
from fishsense_services_api.species_store import (
    SPECIES_PREPROCESS_COHORT,
    SPECIES_PREPROCESS_WORK,
)
from fishsense_services_api.taxonomy_sql import SLATE_CONTENT_MARKER

revision = "0029"
down_revision = "0028"

VIEW = "dive_pipeline_status"
FUNCTION = f"{VIEW}_rows"

#: The stages' current predictor versions, frozen into the view: the laser
#: store's `LASER_PREDICTOR_VERSION` and the contracts'
#: `HEADTAIL_PREDICTOR_VERSION` (the API does not depend on the contracts, so
#: the orchestrator passes it to the head/tail selector). Pinned to both by
#: tests/test_dive_pipeline_status_view.py.
LASER_PREDICTOR_VERSION = 2
HEADTAIL_PREDICTOR_VERSION = 2

_MARKER = f"'{SLATE_CONTENT_MARKER}'"


def _canonical(condition: str) -> str:
    """Dive `d` has a canonical capture `c` for which `condition` holds."""
    return f"""EXISTS (
        SELECT 1 FROM captures c
        WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
          AND {condition}
    )"""


def _has(table: str, alias: str, condition: str) -> str:
    """Capture `c` has a row of `table` (aliased) for which `condition` holds."""
    return f"""EXISTS (
        SELECT 1 FROM {table} {alias}
        WHERE {alias}.tenant_id = c.tenant_id AND {alias}.capture_id = c.id
          AND {condition}
    )"""


def _labeling_complete(table: str) -> str:
    """v1's "complete": a live completed row on a canonical capture, and no
    live incomplete one (views.py `*_labeling_complete`)."""
    done = _canonical(_has(table, "x", "x.completed AND NOT x.superseded"))
    open_ = _canonical(_has(table, "x", "NOT x.completed AND NOT x.superseded"))
    return f"({done} AND NOT {open_})"


def _pending(cohort: str) -> str:
    return f"(d.priority = 'high' AND {cohort})"


_VALID_LASER = _has("laser_labels", "l", VALID_LASER)
_IN_PREDICTION_CLUSTER = """EXISTS (
    SELECT 1 FROM dive_frame_cluster_captures m
    JOIN dive_frame_clusters k ON k.tenant_id = m.tenant_id AND k.id = m.cluster_id
    WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
      AND k.formed_by = 'prediction'
)"""
_LIVE_MARKER = _has(
    "species_labels",
    "sp",
    f"sp.content_of_image = {_MARKER} AND NOT sp.superseded",
)
#: The capture's current server measurement, looked up per capture (0028: a
#: `NOT EXISTS` over `current_measurements` is planned as a scan of the view).
_CURRENTLY_MEASURED = """EXISTS (
    SELECT 1 FROM captures c
    CROSS JOIN LATERAL (
        SELECT 1 FROM current_measurements m
        WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
          AND m.source = 'server'
        LIMIT 1
    ) measured
    WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
)"""
_EFFECTIVE = """(
    SELECT e.borrowed FROM effective_laser_calibrations e
    WHERE e.tenant_id = d.tenant_id AND e.dive_id = d.id
)"""

#: (column, type, SQL over dive `d`), in v1's order, then v2's.
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("dive_id", "bigint", "d.number"),
    ("dive_name", "text", "d.name"),
    ("priority", "text", "upper(d.priority)"),
    (
        "dive_slate_id",
        "bigint",
        "(SELECT st.number FROM slate_templates st WHERE st.id = d.slate_template_id)",
    ),
    # Stage 0.1: every canonical capture has its laser JPEG.
    (
        "laser_preprocessed",
        "boolean",
        f"({_canonical('true')} AND NOT {LASER_PREPROCESS_WORK})",
    ),
    ("laser_labeling_complete", "boolean", _labeling_complete("laser_labels")),
    # Stage 5.1: every valid-laser canonical capture has its head/tail task.
    (
        "headtail_preprocessed",
        "boolean",
        f"({_canonical(_VALID_LASER)} AND NOT {HEADTAIL_PREPROCESS_WORK})",
    ),
    ("headtail_labeling_complete", "boolean", _labeling_complete("head_tail_labels")),
    # Stage 1: clustering ran and persisted its output.
    (
        "has_prediction_clusters",
        "boolean",
        """EXISTS (
            SELECT 1 FROM dive_frame_clusters k
            WHERE k.tenant_id = d.tenant_id AND k.dive_id = d.id
              AND k.formed_by = 'prediction'
        )""",
    ),
    # Stage 2: every processable capture (valid laser, in a prediction
    # cluster) has its species task.
    (
        "dive_images_preprocessed",
        "boolean",
        f"({_canonical(f'{_VALID_LASER} AND {_IN_PREDICTION_CLUSTER}')}"
        f" AND NOT {SPECIES_PREPROCESS_WORK})",
    ),
    ("species_labeling_complete", "boolean", _labeling_complete("species_labels")),
    # Stage 9: every marked canonical capture has its slate task.
    ("slate_applicable", "boolean", "d.slate_template_id IS NOT NULL"),
    (
        "slate_preprocessed",
        "boolean",
        f"(d.slate_template_id IS NOT NULL AND {_canonical(_LIVE_MARKER)}"
        f" AND NOT {slate_preprocess_work(_MARKER)})",
    ),
    ("slate_labeling_complete", "boolean", _labeling_complete("slate_labels")),
    # Stage 13: what the dive is measured with (0018).
    ("calibrated", "boolean", f"{_EFFECTIVE} IS NOT NULL"),
    (
        "calibration_source",
        "text",
        f"CASE {_EFFECTIVE} WHEN false THEN 'own' WHEN true THEN 'borrowed'"
        " ELSE 'none' END",
    ),
    # Stage 14: measured under today's calibration, nothing left to measure.
    (
        "measured",
        "boolean",
        f"({_CURRENTLY_MEASURED} AND NOT {MEASUREMENT_COHORT})",
    ),
    # v2: each selector's cohort.
    ("laser_preprocess_pending", "boolean", _pending(LASER_PREPROCESS_COHORT)),
    (
        "laser_prediction_pending",
        "boolean",
        _pending(laser_prediction_cohort(str(LASER_PREDICTOR_VERSION))),
    ),
    ("clustering_pending", "boolean", _pending(CLUSTERING_COHORT)),
    ("species_preprocess_pending", "boolean", _pending(SPECIES_PREPROCESS_COHORT)),
    ("headtail_preprocess_pending", "boolean", _pending(HEADTAIL_PREPROCESS_COHORT)),
    (
        "headtail_prediction_pending",
        "boolean",
        _pending(headtail_prediction_cohort(str(HEADTAIL_PREDICTOR_VERSION))),
    ),
    (
        "slate_preprocess_pending",
        "boolean",
        _pending(slate_preprocess_cohort(_MARKER)),
    ),
    ("laser_calibration_pending", "boolean", _pending(LASER_CALIBRATION_COHORT)),
    (
        "checkerboard_calibration_pending",
        "boolean",
        _pending(CHECKERBOARD_CALIBRATION_COHORT),
    ),
    ("laser_depth_pending", "boolean", _pending(LASER_DEPTH_COHORT)),
    ("measurement_pending", "boolean", _pending(MEASUREMENT_COHORT)),
)

_SELECT = ",\n".join(f"({sql}) AS {name}" for name, _, sql in COLUMNS)
_RETURNS = ", ".join(f"{name} {type_}" for name, type_, _ in COLUMNS)

#: The rows. SECURITY INVOKER (the default): RLS is the caller's.
FUNCTION_SQL = f"""
    CREATE FUNCTION {FUNCTION}() RETURNS TABLE ({_RETURNS})
    LANGUAGE sql STABLE SET jit = off AS $$
        SELECT {_SELECT}
        FROM dives d
    $$
"""

VIEW_SQL = f"""
    CREATE VIEW {VIEW} WITH (security_invoker = true) AS
    SELECT * FROM {FUNCTION}()
"""

#: What `FUNCTION_SQL` rendered to when this migration was written. A store
#: predicate that changes since is not in any existing database's function:
#: ship a migration that recreates it (see the module docstring).
RENDERED_SHA256 = "6ed3c44fc4af28a18b025e14add4f0da4df867a014a89dda1e220950d6bcf9d7"

_VALID_LASER_0026 = (
    "{t}.completed AND NOT {t}.superseded AND {t}.x IS NOT NULL AND {t}.y IS NOT NULL"
)

#: 0026's `laser_depth_work`, with "the capture has no current depth still
#: good" given as a join and a condition.
_LASER_DEPTH_WORK = """
    CREATE OR REPLACE VIEW laser_depth_work WITH (security_invoker = true) AS
        SELECT c.tenant_id,
               c.dive_id,
               c.id AS capture_id,
               c.number AS capture_number,
               l.id AS laser_label_id,
               l.number AS laser_label_number,
               l.x,
               l.y,
               g.laser_calibration_id
        FROM captures c
        JOIN dive_laser_geometry g
          ON g.tenant_id = c.tenant_id AND g.dive_id = c.dive_id
        JOIN laser_labels l
          ON l.tenant_id = c.tenant_id AND l.capture_id = c.id
        {current_join}
        WHERE c.is_canonical
          AND {valid_l}
          {current_where}
          AND NOT EXISTS (
              SELECT 1 FROM laser_depth_refusals r
              WHERE r.tenant_id = l.tenant_id AND r.capture_id = c.id
                AND r.laser_label_id = l.id
                AND r.laser_calibration_id = g.laser_calibration_id
                AND r.laser_x = l.x AND r.laser_y = l.y
          )
"""
LASER_DEPTH_WORK = _LASER_DEPTH_WORK.format(
    valid_l=_VALID_LASER_0026.format(t="l"),
    # Per capture, with the capture pushed into the view: an index lookup.
    current_join="""LEFT JOIN LATERAL (
            SELECT cd.laser_label_id, cd.laser_calibration_id
            FROM current_laser_depths cd
            WHERE cd.tenant_id = c.tenant_id AND cd.capture_id = c.id
            LIMIT 1
        ) cd ON true""",
    current_where=f"""AND NOT coalesce(
              cd.laser_calibration_id = g.laser_calibration_id
              AND EXISTS (
                  SELECT 1 FROM laser_labels rl
                  WHERE rl.tenant_id = c.tenant_id AND rl.id = cd.laser_label_id
                    AND rl.capture_id = c.id
                    AND {_VALID_LASER_0026.format(t="rl")}
              ),
              false)""",
)
LASER_DEPTH_WORK_0026 = _LASER_DEPTH_WORK.format(
    valid_l=_VALID_LASER_0026.format(t="l"),
    current_join="",
    current_where=f"""AND NOT EXISTS (
              SELECT 1 FROM current_laser_depths cd
              JOIN laser_labels rl
                ON rl.tenant_id = cd.tenant_id AND rl.id = cd.laser_label_id
              WHERE cd.tenant_id = c.tenant_id AND cd.capture_id = c.id
                AND cd.laser_calibration_id = g.laser_calibration_id
                AND rl.capture_id = c.id
                AND {_VALID_LASER_0026.format(t="rl")}
          )""",
)


def _current_laser_depths(distinct_on: str) -> str:
    """0013's `current_laser_depths`, its columns spelled as Postgres froze
    them (`number`, 0019, came after)."""
    return f"""
        CREATE OR REPLACE VIEW current_laser_depths WITH (security_invoker = true) AS
            SELECT DISTINCT ON ({distinct_on})
                   id, tenant_id, v1_id, seq, capture_id, laser_label_id,
                   laser_calibration_id, depth_m, range_m, residual_m,
                   core_version, created_at
            FROM laser_depths
            ORDER BY {distinct_on}, seq DESC
    """


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(
        "CREATE INDEX captures_tenant_id_dive_id_idx ON captures (tenant_id, dive_id)"
    )
    op.execute(
        "CREATE INDEX laser_depths_tenant_id_capture_id_idx "
        "ON laser_depths (tenant_id, capture_id)"
    )
    # A capture belongs to one tenant, so the rows are the same.
    op.execute(_current_laser_depths("tenant_id, capture_id"))
    op.execute(LASER_DEPTH_WORK)

    op.execute(FUNCTION_SQL)
    op.execute(VIEW_SQL)
    op.execute(f"GRANT SELECT ON {VIEW} TO {_app_role()}")


def downgrade() -> None:
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"DROP FUNCTION {FUNCTION}()")
    op.execute(LASER_DEPTH_WORK_0026)
    op.execute(_current_laser_depths("capture_id"))
    op.execute("DROP INDEX laser_depths_tenant_id_capture_id_idx")
    op.execute("DROP INDEX captures_tenant_id_dive_id_idx")
