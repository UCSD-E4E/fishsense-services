"""The laser-depth and stage-14 work, as views; and v1's binding rule.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
controllers/dive_cohort_controller.py (`_laser_depth_cohort_query`,
`select_next_for_measure_fish`, `_valid_laser_conditions`,
`_valid_headtail_conditions`, `_measurable_species_conditions`,
`_is_fish_model_condition`, `_resolved_laser_extrinsics_id`) and from the
decisions measure_fish_activity.py made per image.

**Why views.** In v1 the cohort (SQL) and the activity (Python) each decided
what counted as work, and every disagreement was a dive re-selected forever
(dives 32, 60, 279, 466; the `Fish Model,` empty leaf). Here one definition per
stage -- a row per unit of work -- serves the cohort (`EXISTS`), the resolver
(the rows themselves) and the persist check (is this still work?), and the
pipeline-status view can read the same rows.

- ``dive_laser_geometry``: what a dive is measured with -- its effective laser
  calibration (0018: own, plausible, else the link's) and the current camera
  calibration of its device. Only dives the kernel can project: a pinhole
  calibration (it projects through K^-1 and nothing else) with an invertible
  matrix, and a laser axis with a direction (fishsense-core refuses a zero
  one, for every image of the dive alike). v1 raised in the activity for
  these and retried for an hour, at the head of the cohort.
- ``laser_depth_work``: per canonical capture with no current depth, each
  valid laser label not yet refused under the effective calibration. A depth
  is current when the capture's latest one names a still-valid label **of
  the same capture** under the effective calibration (v1: dive 279's fix and
  the 2026-08-20 correlation outage).
- ``measurement_subjects``: per capture, the one species label stage 14 reads
  -- live (not superseded), not a sentinel, the highest-numbered -- what it
  names (a real fish, or a model or calibration target by name), and the
  capture's Label Studio cluster (the highest-numbered, as v1's per-image
  index kept the last) with the fish it is bound to.
- ``measurement_work``: per canonical capture v1's stage 14 would measure --
  a top-three measurable subject, a valid laser label and a valid head/tail
  label (the lowest-numbered of each), a cluster for a real fish -- with no
  current server measurement and no refusal of these very inputs.

**current_measurements** (0013) gains v1's binding rule (fishsense-lite #527,
#905). v1 upserted on (image, fish), so a relabel or a re-cluster that
changed the fish would have *added* a row beside the old one, and its
activity DELETEd the old binding first. Measurements are append-only here;
instead a server measurement is not current while the capture's subject names
a different fish -- a real fish's cluster bound to another fish, or a model
label naming another model. The row stays as history, and the frame is counted
once (prod dives 341/383). As in v1, a species relabel of a real fish changes
nothing: its identity is the cluster's.

Revision ID: 0026
Revises: 0025
"""

from alembic import context, op

revision = "0026"
down_revision = "0025"

VALID_LASER = (
    "{t}.completed AND NOT {t}.superseded AND {t}.x IS NOT NULL AND {t}.y IS NOT NULL"
)
VALID_HEAD_TAIL = (
    "{t}.completed AND NOT {t}.superseded AND {t}.head_x IS NOT NULL"
    " AND {t}.head_y IS NOT NULL AND {t}.tail_x IS NOT NULL AND {t}.tail_y IS NOT NULL"
)
#: fishsense-lite taxonomy.REAL_FISH_LIKE: "contains ( and ends with )".
REAL_FISH = "coalesce({c} LIKE '%(%)', false)"

VIEWS = (
    "measurement_work",
    "measurement_subjects",
    "laser_depth_work",
    "dive_laser_geometry",
)

CURRENT_MEASUREMENTS_0013 = """
    CREATE OR REPLACE VIEW current_measurements WITH (security_invoker = true) AS
        SELECT DISTINCT ON (m.capture_id, m.fish_id, m.source) m.*
        FROM measurements m
        JOIN captures c
          ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
        LEFT JOIN effective_laser_calibrations e
          ON e.tenant_id = c.tenant_id AND e.dive_id = c.dive_id
        LEFT JOIN laser_labels ll
          ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
        LEFT JOIN head_tail_labels ht
          ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
        WHERE (m.source = 'device'
               OR m.laser_calibration_id = e.laser_calibration_id)
          AND NOT coalesce(ll.superseded, false)
          AND NOT coalesce(ht.superseded, false)
        ORDER BY m.capture_id, m.fish_id, m.source, m.seq DESC
    """


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _functions() -> None:
    # A jsonb 3-array of numbers: what a laser vector or a matrix row must be
    # before anything casts it.
    op.execute("""
        CREATE FUNCTION jsonb_numeric_triple(v jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT coalesce(
                jsonb_typeof(v) = 'array' AND jsonb_array_length(v) = 3
                AND jsonb_typeof(v -> 0) = 'number'
                AND jsonb_typeof(v -> 1) = 'number'
                AND jsonb_typeof(v -> 2) = 'number',
                false)
        $$
        """)
    # CASE, not AND: SQL does not promise to evaluate the type test before
    # the casts.
    op.execute("""
        CREATE FUNCTION usable_laser_axis(axis jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE WHEN jsonb_numeric_triple(axis)
                THEN power((axis ->> 0)::double precision, 2)
                   + power((axis ->> 1)::double precision, 2)
                   + power((axis ->> 2)::double precision, 2) > 0
                ELSE false END
        $$
        """)
    op.execute("""
        CREATE FUNCTION usable_camera_matrix(k jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE
                WHEN jsonb_numeric_triple(k -> 0) AND jsonb_numeric_triple(k -> 1)
                 AND jsonb_numeric_triple(k -> 2)
                THEN (k -> 0 ->> 0)::double precision * (
                         (k -> 1 ->> 1)::double precision * (k -> 2 ->> 2)::double precision
                       - (k -> 1 ->> 2)::double precision * (k -> 2 ->> 1)::double precision)
                   - (k -> 0 ->> 1)::double precision * (
                         (k -> 1 ->> 0)::double precision * (k -> 2 ->> 2)::double precision
                       - (k -> 1 ->> 2)::double precision * (k -> 2 ->> 0)::double precision)
                   + (k -> 0 ->> 2)::double precision * (
                         (k -> 1 ->> 0)::double precision * (k -> 2 ->> 1)::double precision
                       - (k -> 1 ->> 1)::double precision * (k -> 2 ->> 0)::double precision)
                   <> 0
                ELSE false END
        $$
        """)
    # fishsense-lite taxonomy.parse_model_name, in SQL: the rigid known-length
    # target a `content_of_image` names, or NULL. The calibration targets are
    # an allowlist (the checkerboard is a plane, not a length); a model's
    # name is its leaf with spaces trimmed (SQL TRIM and `.strip(" ")` agree),
    # and an empty leaf names nothing. tests/test_depth_measure_schema.py pins
    # it to the Python parser and to `rigid_target_sql` over the shared corpus.
    op.execute("""
        CREATE FUNCTION fish_model_name(content_of_image text)
        RETURNS text LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE
                WHEN content_of_image = 'Calibration Targets, Ruler' THEN 'Ruler'
                WHEN content_of_image = 'Calibration Targets, Box' THEN 'Box'
                WHEN content_of_image LIKE 'Fish Model,%'
                    THEN NULLIF(btrim(substr(content_of_image, 12), ' '), '')
            END
        $$
        """)


def upgrade() -> None:
    app_role = _app_role()
    _functions()

    op.execute("""
        CREATE VIEW dive_laser_geometry WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS dive_id,
                   lc.id AS laser_calibration_id,
                   lc.laser_position,
                   lc.laser_axis,
                   cc.id AS camera_calibration_id,
                   cc.camera_matrix
            FROM dives d
            JOIN effective_laser_calibrations e
              ON e.tenant_id = d.tenant_id AND e.dive_id = d.id
            JOIN laser_calibrations lc
              ON lc.tenant_id = e.tenant_id AND lc.id = e.laser_calibration_id
            JOIN current_camera_calibrations cc
              ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
            WHERE cc.camera_model = 'pinhole'
              AND jsonb_numeric_triple(lc.laser_position)
              AND usable_laser_axis(lc.laser_axis)
              AND usable_camera_matrix(cc.camera_matrix)
        """)

    op.execute(f"""
        CREATE VIEW laser_depth_work WITH (security_invoker = true) AS
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
            WHERE c.is_canonical
              AND {VALID_LASER.format(t="l")}
              AND NOT EXISTS (
                  SELECT 1 FROM current_laser_depths cd
                  JOIN laser_labels rl
                    ON rl.tenant_id = cd.tenant_id AND rl.id = cd.laser_label_id
                  WHERE cd.tenant_id = c.tenant_id AND cd.capture_id = c.id
                    AND cd.laser_calibration_id = g.laser_calibration_id
                    AND rl.capture_id = c.id
                    AND {VALID_LASER.format(t="rl")}
              )
              AND NOT EXISTS (
                  SELECT 1 FROM laser_depth_refusals r
                  WHERE r.tenant_id = l.tenant_id AND r.capture_id = c.id
                    AND r.laser_label_id = l.id
                    AND r.laser_calibration_id = g.laser_calibration_id
                    AND r.laser_x = l.x AND r.laser_y = l.y
              )
        """)

    op.execute(f"""
        CREATE VIEW measurement_subjects WITH (security_invoker = true) AS
            SELECT DISTINCT ON (s.tenant_id, s.capture_id)
                   s.tenant_id,
                   s.capture_id,
                   s.id AS species_label_id,
                   s.content_of_image,
                   coalesce(s.top_three_photos_of_group, false) AS top_three,
                   {REAL_FISH.format(c="s.content_of_image")} AS real_fish,
                   CASE WHEN NOT {REAL_FISH.format(c="s.content_of_image")}
                        THEN fish_model_name(s.content_of_image)
                   END AS model_name,
                   k.id AS cluster_id,
                   k.fish_id AS cluster_fish_id
            FROM species_labels s
            LEFT JOIN LATERAL (
                SELECT k.id, k.fish_id
                FROM dive_frame_cluster_captures kc
                JOIN dive_frame_clusters k
                  ON k.tenant_id = kc.tenant_id AND k.id = kc.cluster_id
                WHERE kc.tenant_id = s.tenant_id AND kc.capture_id = s.capture_id
                  AND k.formed_by = 'label_studio'
                ORDER BY k.number DESC
                LIMIT 1
            ) k ON true
            WHERE NOT s.superseded AND s.ls_project_id IS NOT NULL
            ORDER BY s.tenant_id, s.capture_id, s.number DESC
        """)

    op.execute("""
        CREATE OR REPLACE VIEW current_measurements WITH (security_invoker = true) AS
            SELECT DISTINCT ON (m.capture_id, m.fish_id, m.source) m.*
            FROM measurements m
            JOIN captures c
              ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            LEFT JOIN effective_laser_calibrations e
              ON e.tenant_id = c.tenant_id AND e.dive_id = c.dive_id
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
              -- v1's stale binding: the capture's subject names another fish.
              AND NOT (m.source = 'server' AND m.fish_id IS NOT NULL AND coalesce(
                    (s.real_fish AND s.cluster_fish_id IS NOT NULL
                     AND s.cluster_fish_id <> m.fish_id)
                 OR (NOT s.real_fish AND s.model_name IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM fish f
                        JOIN fish_models fm ON fm.id = f.fish_model_id
                        WHERE f.tenant_id = m.tenant_id AND f.id = m.fish_id
                          AND fm.name = s.model_name)),
                    false))
            ORDER BY m.capture_id, m.fish_id, m.source, m.seq DESC
        """)

    op.execute(f"""
        CREATE VIEW measurement_work WITH (security_invoker = true) AS
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
            WHERE c.is_canonical
              AND s.top_three
              AND (s.real_fish OR s.model_name IS NOT NULL)
              -- Real fish are identified by their Label Studio cluster; a
              -- model or target by its name, so it needs none (v1).
              AND (NOT s.real_fish OR s.cluster_id IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM current_measurements m
                  WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                    AND m.source = 'server'
              )
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
        """)

    op.execute(f"GRANT SELECT ON {', '.join(VIEWS)} TO {app_role}")


def downgrade() -> None:
    op.execute("DROP VIEW measurement_work")
    op.execute(CURRENT_MEASUREMENTS_0013)
    op.execute("DROP VIEW measurement_subjects")
    op.execute("DROP VIEW laser_depth_work")
    op.execute("DROP VIEW dive_laser_geometry")
    op.execute("DROP FUNCTION fish_model_name(text)")
    op.execute("DROP FUNCTION usable_camera_matrix(jsonb)")
    op.execute("DROP FUNCTION usable_laser_axis(jsonb)")
    op.execute("DROP FUNCTION jsonb_numeric_triple(jsonb)")
