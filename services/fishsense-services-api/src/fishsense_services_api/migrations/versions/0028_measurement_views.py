"""v1's stale-binding rule only where v1 applied it, and stage 14's work in
linear time.

**The stale binding is stage 14's to retire.** 0026 gave `current_measurements`
v1's binding rule (fishsense-lite #527, #905): a server measurement bound to a
fish the capture's subject no longer names is not current. But v1 enforced it
by a DELETE in measure_fish_activity.py (fishsense-lite@77e8f8e5), which ran
only on dives its cohort selected -- high priority (dive_cohort_controller.py
`select_next_for_measure_fish`). On a low-priority dive the stale row stayed,
and v1's portal and research exports kept counting it. 0026 hid it on every
dive, while only a high-priority dive is ever re-measured, so the frame read
as unmeasured and stayed so. The rule now holds on high-priority dives only:
exactly the dives stage 14 will re-measure, and exactly where v1 would have
deleted. Flip a dive to high and its stale rows stop counting, as v1's next
measure run would have made them.

**Linear, not quadratic.** `measurement_work` asked `NOT EXISTS (SELECT ...
FROM current_measurements ...)` per capture. `current_measurements` is a
`DISTINCT ON` view that cannot be flattened, so Postgres materialised all of
it and rescanned it for every capture (a nested-loop anti join): 30 s for a
cohort pass at 40k captures. `measurement_subjects` did the same to
`dive_frame_cluster_captures`, which had no index on the capture. Now:

- `measurement_work` reads the capture's current server measurement through
  a `LATERAL ... LIMIT 1`, which Postgres evaluates per capture with the
  capture pushed into the view: an index lookup, not a scan. The rule is
  still `current_measurements`' alone -- nothing is restated here;
- `current_measurements` names `tenant_id` among its `DISTINCT ON` keys (a
  capture belongs to one tenant, so the rows are the same), so a lookup by
  (tenant, capture) reaches the index on both columns;
- indexes on `measurements (tenant_id, capture_id)` and
  `dive_frame_cluster_captures (tenant_id, capture_id)`. `species_labels`,
  `laser_labels` and `head_tail_labels` need none: their
  `UNIQUE (tenant_id, capture_id, ls_project_id)` key already leads with the
  pair.

Additive: two views replaced with the same columns, two indexes.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"

#: v1's stale binding (0026): the capture's subject names another fish -- a
#: real fish's cluster bound to another fish, or a model label naming another
#: model. `m` is the measurement, `s` the capture's top-three subject.
_STALE_BINDING = """
    m.source = 'server' AND m.fish_id IS NOT NULL AND coalesce(
        (s.real_fish AND s.cluster_fish_id IS NOT NULL
         AND s.cluster_fish_id <> m.fish_id)
     OR (NOT s.real_fish AND s.model_name IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM fish f
            JOIN fish_models fm ON fm.id = f.fish_model_id
            WHERE f.tenant_id = m.tenant_id AND f.id = m.fish_id
              AND fm.name = s.model_name)),
        false)
"""


def _current_measurements(
    *, distinct_on: str, effective_join: str, dive_join: str, stale_where: str
) -> str:
    return f"""
        CREATE OR REPLACE VIEW current_measurements WITH (security_invoker = true) AS
            SELECT DISTINCT ON ({distinct_on}) m.*
            FROM measurements m
            JOIN captures c
              ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            {dive_join}
            {effective_join}
            LEFT JOIN laser_labels ll
              ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
            LEFT JOIN head_tail_labels ht
              ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
            LEFT JOIN measurement_subjects s
              ON s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
             AND s.top_three
            WHERE (m.source = 'device'
                   OR m.laser_calibration_id = e.laser_calibration_id)
              AND NOT coalesce(ll.superseded, false)
              AND NOT coalesce(ht.superseded, false)
              AND NOT ({stale_where})
            ORDER BY {distinct_on}, m.seq DESC
        """


CURRENT_MEASUREMENTS = _current_measurements(
    distinct_on="m.tenant_id, m.capture_id, m.fish_id, m.source",
    # Per row, with the dive pushed into the view (one row per dive; the
    # LIMIT keeps the planner from flattening it back into a join, which
    # evaluates every dive's effective calibration per capture looked up).
    effective_join="""LEFT JOIN LATERAL (
                SELECT e.laser_calibration_id FROM effective_laser_calibrations e
                WHERE e.tenant_id = c.tenant_id AND e.dive_id = c.dive_id
                LIMIT 1
            ) e ON true""",
    # A capture may have no dive (captures.dive_id is nullable): its rows
    # must not drop out of the view for that.
    dive_join="""LEFT JOIN dives d
              ON d.tenant_id = c.tenant_id AND d.id = c.dive_id""",
    # Only where stage 14 runs -- and so where v1 deleted (see above).
    stale_where=f"coalesce(d.priority = 'high', false) AND {_STALE_BINDING}",
)
CURRENT_MEASUREMENTS_0026 = _current_measurements(
    distinct_on="m.capture_id, m.fish_id, m.source",
    effective_join="""LEFT JOIN effective_laser_calibrations e
              ON e.tenant_id = c.tenant_id AND e.dive_id = c.dive_id""",
    dive_join="",
    stale_where=_STALE_BINDING,
)

VALID_LASER = (
    "{t}.completed AND NOT {t}.superseded AND {t}.x IS NOT NULL AND {t}.y IS NOT NULL"
)
VALID_HEAD_TAIL = (
    "{t}.completed AND NOT {t}.superseded AND {t}.head_x IS NOT NULL"
    " AND {t}.head_y IS NOT NULL AND {t}.tail_x IS NOT NULL AND {t}.tail_y IS NOT NULL"
)

_MEASURED_0026 = """
              AND NOT EXISTS (
                  SELECT 1 FROM current_measurements m
                  WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                    AND m.source = 'server'
              )
"""


def _measurement_work(*, measured_join: str, measured_where: str) -> str:
    """0026's `measurement_work`, with the "no current server measurement"
    test given as a join and a condition."""
    return f"""
        CREATE OR REPLACE VIEW measurement_work WITH (security_invoker = true) AS
            SELECT c.tenant_id,
                   c.dive_id,
                   c.id AS capture_id,
                   c.number AS capture_number,
                   g.laser_calibration_id,
                   s.species_label_id,
                   s.content_of_image,
                   s.real_fish,
                   s.model_name,
                   s.cluster_id,
                   s.cluster_fish_id,
                   ll.id AS laser_label_id,
                   ll.x AS laser_x,
                   ll.y AS laser_y,
                   ht.id AS head_tail_label_id,
                   ht.head_x,
                   ht.head_y,
                   ht.tail_x,
                   ht.tail_y
            FROM captures c
            JOIN dive_laser_geometry g
              ON g.tenant_id = c.tenant_id AND g.dive_id = c.dive_id
            JOIN measurement_subjects s
              ON s.tenant_id = c.tenant_id AND s.capture_id = c.id
            CROSS JOIN LATERAL (
                SELECT l.id, l.x, l.y FROM laser_labels l
                WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id
                  AND {VALID_LASER.format(t="l")}
                ORDER BY l.number
                LIMIT 1
            ) ll
            CROSS JOIN LATERAL (
                SELECT h.id, h.head_x, h.head_y, h.tail_x, h.tail_y
                FROM head_tail_labels h
                WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                  AND {VALID_HEAD_TAIL.format(t="h")}
                ORDER BY h.number
                LIMIT 1
            ) ht
            {measured_join}
            WHERE c.is_canonical
              AND s.top_three
              AND (s.real_fish OR s.model_name IS NOT NULL)
              -- Real fish are identified by their Label Studio cluster; a
              -- model or target by its name, so it needs none (v1).
              AND (NOT s.real_fish OR s.cluster_id IS NOT NULL)
              {measured_where}
              AND NOT EXISTS (
                  SELECT 1 FROM measurement_refusals r
                  WHERE r.tenant_id = c.tenant_id AND r.capture_id = c.id
                    AND r.laser_calibration_id = g.laser_calibration_id
                    AND r.laser_label_id = ll.id
                    AND r.laser_x = ll.x AND r.laser_y = ll.y
                    AND r.head_tail_label_id = ht.id
                    AND r.head_x = ht.head_x AND r.head_y = ht.head_y
                    AND r.tail_x = ht.tail_x AND r.tail_y = ht.tail_y
                    AND (r.reason <> 'unparseable_species'
                         OR (r.species_label_id = s.species_label_id
                             AND r.content_of_image IS NOT DISTINCT FROM
                                 s.content_of_image))
              )
        """


MEASUREMENT_WORK = _measurement_work(
    # Per capture, with the capture pushed into current_measurements: an
    # index lookup. `NOT EXISTS` here is planned as an anti join against the
    # whole view, materialised and rescanned for every capture.
    measured_join="""
            LEFT JOIN LATERAL (
                SELECT m.id FROM current_measurements m
                WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                  AND m.source = 'server'
                LIMIT 1
            ) measured ON true
    """,
    measured_where="AND measured.id IS NULL",
)
MEASUREMENT_WORK_0026 = _measurement_work(
    measured_join="", measured_where=_MEASURED_0026
)


def upgrade() -> None:
    op.execute(
        "CREATE INDEX measurements_tenant_id_capture_id_idx "
        "ON measurements (tenant_id, capture_id)"
    )
    op.execute(
        "CREATE INDEX dive_frame_cluster_captures_tenant_id_capture_id_idx "
        "ON dive_frame_cluster_captures (tenant_id, capture_id)"
    )
    op.execute(CURRENT_MEASUREMENTS)
    op.execute(MEASUREMENT_WORK)


def downgrade() -> None:
    op.execute(MEASUREMENT_WORK_0026)
    op.execute(CURRENT_MEASUREMENTS_0026)
    op.execute("DROP INDEX dive_frame_cluster_captures_tenant_id_capture_id_idx")
    op.execute("DROP INDEX measurements_tenant_id_capture_id_idx")
