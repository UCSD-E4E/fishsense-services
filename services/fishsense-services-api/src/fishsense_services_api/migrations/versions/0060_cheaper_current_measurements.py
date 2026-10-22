"""A cheaper `current_measurements`: the same rows, asked in a way the planner
can estimate and that does no work a row's rules cannot need.

0045 left this open ("Faster pages need a cheaper ``current_measurements``, a
change of its own"). On production (read-only, as the app role, JIT off,
2026-10-21; lab: 3,319 measurements, 3,314 current, 134,662 captures) the
Today page's fish list took 3.0-3.4 s, of which the view itself took
0.7-0.9 s. The rest came from the joins above it, planned for the 24 rows the
planner expected rather than the 3,314 it got. `fish` and `missions` were
joined by testing every pair: 732k and 867k rows were thrown away, about
2.4 s.

The estimate collapsed because each rule was a filter on a left-joined
column, which Postgres has no statistics for. It falls back to a default
guess, so each rule appeared to remove rows that none of them did:

    measurements x captures        est 3423   actual 3319
    the effective calibration      est  110   actual 3319
    NOT coalesce(ht.superseded)    est   55   actual 3319
    NOT coalesce(ll.superseded)    est   28   actual 3319
    the stale binding              est   24   actual 3314

The stale binding (0028) also joined `measurement_subjects` for the whole
tenant, which is every species label's top-three subject and cluster: about
390 ms. It did so even though the rule can only hold on a high-priority
mission's server measurement.

Now, with the same rules:

- **A superseded label is an anti-join** (`NOT EXISTS ... AND superseded`),
  which the planner estimates from the label tables' statistics. The left
  join was on the label's key, so at most one label per row: the same rows.
- **The stale binding is an `EXISTS`, asked last.** The `AND` puts it after
  the mission's priority, the source and the fish, so it runs only where
  0028's rule can hold, and it looks up that capture's subject alone.
  ``coalesce(<rule>, false)`` over the left-joined subject becomes "a subject
  exists for which the rule is true". With no subject, or a rule that is
  NULL, both forms say "not stale". `measurement_subjects` is one row per
  capture (its `DISTINCT ON`), and `top_three` is filtered after it, in both
  forms.
- The effective-calibration check stays as it was. Its estimate is still
  low, at 83 against 3,314, because a column equal to another table's
  column is guessed from distinct counts. Hiding it from the planner
  (C2 below) did not help.

Measured on production, with each candidate as a temp view inside a
transaction that was rolled back:

    candidate        rows vs live view           fish list    view alone
    live (0038)      --                          3.0-3.3 s    0.68-0.70 s
    C1 (this)        0 differ, either direction  0.78-0.80 s  0.57-0.60 s
    C2 (C1 + opaque  0 differ, either direction  0.86-1.13 s  0.64-0.87 s
        calibration
        check)

The comparison covered `current_measurements` and `observations`, every
tenant's rows, using `EXCEPT ALL` both ways. Production has no `device`
measurements, so that branch was compared only by the tests. The rules'
tests are unchanged (test_measurements.py, test_measurement_store.py,
test_depth_measure_schema.py). test_current_measurements_cost.py pins the
work.

Same columns, so `observations`, `measurement_work` and
`automatic_results_export` are unchanged, and so are their grants.

Revision ID: 0060
Revises: 0059
"""

from alembic import op

revision = "0060"
down_revision = "0059"

_HEAD = """
CREATE OR REPLACE VIEW current_measurements WITH (security_invoker = true) AS
SELECT DISTINCT ON (m.tenant_id, m.capture_id, m.fish_id, m.source)
    {columns}
FROM measurements m
JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
LEFT JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
-- Per row, with the mission pushed in (0028).
LEFT JOIN LATERAL (
    SELECT e.laser_calibration_id FROM effective_laser_calibrations e
    WHERE e.tenant_id = c.tenant_id AND e.mission_id = c.mission_id
    LIMIT 1
) e ON true
"""

_ORDER = """
ORDER BY m.tenant_id, m.capture_id, m.fish_id, m.source,
    m.measured_at DESC NULLS LAST, m.seq DESC
"""

#: v1's stale binding (0026, 0028), unchanged: the capture's subject names
#: another fish -- a real fish's cluster bound to another, or a model label
#: naming another model.
_STALE_SUBJECT = """
    (s.real_fish AND s.cluster_fish_id IS NOT NULL
     AND s.cluster_fish_id <> m.fish_id)
 OR (NOT s.real_fish AND s.model_name IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fish f
        JOIN fish_models fm ON fm.id = f.fish_model_id
        WHERE f.tenant_id = m.tenant_id AND f.id = m.fish_id
          AND fm.name = s.model_name))
"""

CURRENT_MEASUREMENTS = _HEAD + f"""
WHERE (m.source = 'device' OR m.laser_calibration_id = e.laser_calibration_id)
  AND NOT EXISTS (
      SELECT 1 FROM laser_labels ll
      WHERE ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
        AND ll.superseded)
  AND NOT EXISTS (
      SELECT 1 FROM head_tail_labels ht
      WHERE ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
        AND ht.superseded)
  -- Only where stage 14 runs (0028), and only then the capture's subject.
  AND NOT (
      coalesce(d.priority = 'high', false)
      AND m.source = 'server' AND m.fish_id IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM measurement_subjects s
          WHERE s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
            AND s.top_three
            AND ({_STALE_SUBJECT})))
""" + _ORDER

#: The view as 0038 left it and 0046 renamed it. A copy, since the text 0038
#: built from (0028's) names `dives`; test_migration_0060.py pins that the
#: downgrade restores 0059's definition exactly.
CURRENT_MEASUREMENTS_0038 = _HEAD + f"""
LEFT JOIN laser_labels ll
  ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
LEFT JOIN head_tail_labels ht
  ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
LEFT JOIN measurement_subjects s
  ON s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
 AND s.top_three
WHERE (m.source = 'device' OR m.laser_calibration_id = e.laser_calibration_id)
  AND NOT coalesce(ll.superseded, false)
  AND NOT coalesce(ht.superseded, false)
  AND NOT (coalesce(d.priority = 'high', false)
           AND m.source = 'server' AND m.fish_id IS NOT NULL
           AND coalesce({_STALE_SUBJECT}, false))
""" + _ORDER


def _columns() -> str:
    """The view's columns as they are, in order: `CREATE OR REPLACE VIEW` must
    keep them (0038 spelled out `measurements`' own, less `measured_at`)."""
    names = op.get_bind().exec_driver_sql("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'current_measurements'
        ORDER BY ordinal_position
        """).scalars()
    return ", ".join(f"m.{name}" for name in names)


def upgrade() -> None:
    op.execute(CURRENT_MEASUREMENTS.format(columns=_columns()))


def downgrade() -> None:
    op.execute(CURRENT_MEASUREMENTS_0038.format(columns=_columns()))
