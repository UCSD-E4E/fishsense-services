"""v1's tables and fish views, v1-shaped, for the research repos.

The research repos are production consumers (PLAN.md §2.7, §9.20): imwut's
`fish_model_analysis/sql/extract_*.sql`, cscw's `sql/extract_*.sql` and its
unscripted extracts, and `pubfig.load_measurements` (an export of
`fish_model_measurement_accuracy`) query v1's tables by name, with v1's ids
hard-coded. v2 renamed and re-keyed everything, so they would all break at
cutover. This keeps them running unchanged: **schema `v1`** holds a view per
v1 table they read, named as v1's table, with v1's columns -- so a research
session changes only its `search_path` (`v1, public`). docs/port-map/
consumers.json lists the renames each view undoes.

**Ids are numbers.** Every v1 id column (`id`, `dive_id`, `image_id`,
`camera_id`, ...) is the referenced row's `number` (0019): v1's id for a
migrated row, the next number above v1's for a new one. v1's hard-coded dive
ids keep naming the same dives.

**v1's rows, not v2's "current".** Where v1 overwrote one row in place and v2
appends (PLAN.md §9.13), the view shows the one row v1 would have held, so the
research numbers reproduce v1's on migrated data (checked against the
2026-09-25 production dump, tests/test_v1_research_views.py on a corpus):

* `measurement`: a capture's latest server measurement. v1 upserted on (image,
  fish) and deleted the image's other bindings when it re-measured
  (fishsense-lite@77e8f8e5 measure_fish_activity.py:300-370), so it held one row
  per image. Unlike `current_measurements` this keeps a measurement whose
  calibration or labels have since changed: v1's views and extracts never
  filtered on freshness (views.py:676-693, extract_field.sql);
* `laserextrinsics`: a dive's latest *accepted* calibration. v1 kept one row
  per dive (`uq_laserextrinsics_dive_id`), overwritten by a refit and left in
  place by a refusal (dive_controller.py:335-365). A measurement made with an
  earlier calibration of its dive names that calibration's number, which is no
  longer this view's row: in v1 the row was overwritten under it, so v1 joined
  the old length to the new geometry;
* `laserdepth`, `laserprediction`, `divelaserline`, `cameraintrinsics`:
  v2's existing `current_*` views -- the latest per capture, dive or device, as
  v1 overwrote them. `cameraintrinsics` shows only a pinhole calibration
  (camera_sql): v1's shape is a pinhole K, and nothing else is one;
* `fishmodelreference`, `calibrationtarget`: the current version per name
  (`current_fish_model_references`, `current_calibration_targets`), so a name
  join never multiplies rows.

**Enums and JSON keep v1's spelling.** `dive.priority` and
`diveframecluster.data_source` are upper case, as v1's enums were (the
research and Superset SQL compare `'HIGH'`, `'PREDICTION'`). v1's `json`
columns are `json` again, so `->>`, `::jsonb` and `::text` all work -- but v2
stored them as `jsonb`, so `::text` shows jsonb's normalised spacing: equal
values, different text.

**Left out** (no research query reads them): v1's tables `headtailprediction`,
`slateprediction`, `labelstudiosynccursor`, `user`; `dive.calibration_refused_*`
(a refusal is a `laser_calibrations` row now); `user_id` on the label tables
(v2 keeps the Label Studio labeler id, not v1's user); and specieslabel's
`laser_x`, `laser_y`, `laser_label`, `slate_upside_down` (never migrated).
tests/test_v1_research_views.py pins this list against v1's schema.

**The three fish views** (fishsense-lite@77e8f8e5 services/fishsense-api/src/
fishsense_api/views.py:676-693, 734-765, 828-869) are v1's SQL over these views,
in `public` where v1 had them: the accuracy per measurement, the per-fish
length estimate (nearest-rank quantiles, kept: `percentile_cont` would change
numbers research compares against) and the mislabel suspects (provisional
references and calibration targets excluded). Their p90 is per fish, not the
paper's per-(dive, model) cell (PLAN.md §9.18).

Every view runs as its caller (`security_invoker`), so RLS decides whose rows
it shows; 0032 grants them to the research role, bound to the lab.
Created in dependency order and dropped in reverse, never CASCADE.

Revision ID: 0031
Revises: 0028
"""

from alembic import op

# Frozen at migration time: this migration renders what these modules say
# today. A later change to them does not reach the views without a migration.
from fishsense_services_api.camera_sql import RECTIFIABLE_CAMERA_MODEL
from fishsense_services_api.taxonomy_sql import calibration_target_name_sql

revision = "0031"
down_revision = "0030"

#: The capture a row `t` belongs to, tenant to tenant.
_CAPTURE = (
    "JOIN public.captures c ON c.tenant_id = {t}.tenant_id AND c.id = {t}.capture_id"
)

#: Row `t` is the one its `current_*` view keeps -- v2's rule, not restated.
#: A semi-join, and the view is driven by the base table: the `DISTINCT ON`
#: views have no statistics for the tenant the RLS policies compare, so
#: selected *from* them the planner guesses one row and re-runs them per row
#: of a research join (the corpus extract took a minute on the production
#: dump).
_CURRENT = "{t}.id IN (SELECT id FROM public.{view})"

#: (name, SELECT) in dependency order. Each is `v1.<name>` unless qualified.
V1_VIEWS: tuple[tuple[str, str], ...] = (
    (
        "camera",
        """
        SELECT d.number AS id, d.serial AS serial_number, d.name
        FROM public.devices d
        """,
    ),
    (
        "species",
        """
        SELECT s.number AS id, s.scientific_name, s.common_name
        FROM public.species s
        """,
    ),
    (
        "calibrationtarget",
        # v1 had one square size; v2 keeps a pitch per axis (the E4E board
        # is ~0.7 % anisotropic). v1's migrated boards have x = y.
        f"""
        SELECT t.number AS id, t.name, t.interior_rows AS rows,
               t.interior_cols AS cols, t.pitch_x_m AS square_size_m, t.notes,
               t.valid_from AS created_at
        FROM public.calibration_targets t
        WHERE {_CURRENT.format(t="t", view="current_calibration_targets")}
        """,
    ),
    (
        "fishmodelreference",
        f"""
        SELECT r.number AS id, r.name, r.known_length_m, r.notes, r.is_provisional
        FROM public.fish_model_references r
        WHERE {_CURRENT.format(t="r", view="current_fish_model_references")}
        """,
    ),
    (
        "diveslate",
        """
        SELECT s.number AS id, s.name, s.source_path AS path, s.created_at, s.dpi,
               s.reference_points::json AS reference_points
        FROM public.slate_templates s
        """,
    ),
    (
        "dive",
        """
        SELECT d.number AS id, d.source_path AS path, d.dived_at AS dive_datetime,
               upper(d.priority) AS priority, dev.number AS camera_id,
               st.number AS dive_slate_id, d.flip_dive_slate, d.name,
               src.number AS calibration_dive_id, d.notes,
               ct.number AS calibration_target_id
        FROM public.dives d
        LEFT JOIN public.devices dev
          ON dev.tenant_id = d.tenant_id AND dev.id = d.device_id
        LEFT JOIN public.slate_templates st ON st.id = d.slate_template_id
        LEFT JOIN public.dives src
          ON src.tenant_id = d.tenant_id AND src.id = d.calibration_source_dive_id
        LEFT JOIN public.calibration_targets ct ON ct.id = d.calibration_target_id
        """,
    ),
    (
        "image",
        """
        SELECT c.number AS id, c.source_path AS path, c.captured_at AS taken_datetime,
               c.checksum, c.is_canonical, d.number AS dive_id,
               dev.number AS camera_id
        FROM public.captures c
        LEFT JOIN public.dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
        LEFT JOIN public.devices dev
          ON dev.tenant_id = c.tenant_id AND dev.id = c.device_id
        """,
    ),
    (
        "fish",
        # A fish is a species' or a model's; v1 named a model by string.
        """
        SELECT f.number AS id, s.number AS species_id, m.name
        FROM public.fish f
        LEFT JOIN public.species s ON s.id = f.species_id
        LEFT JOIN public.fish_models m ON m.id = f.fish_model_id
        """,
    ),
    (
        "cameraintrinsics",
        f"""
        SELECT k.number AS id, k.camera_matrix::json AS camera_matrix,
               k.distortion_coefficients::json AS distortion_coefficients,
               dev.number AS camera_id
        FROM public.camera_calibrations k
        JOIN public.devices dev ON dev.tenant_id = k.tenant_id AND dev.id = k.device_id
        WHERE {_CURRENT.format(t="k", view="current_camera_calibrations")}
          AND k.camera_model = '{RECTIFIABLE_CAMERA_MODEL}'
        """,
    ),
    (
        "laserextrinsics",
        # No accepted calibration of the dive is newer. v1's camera_id: the
        # dive's device (on the 2026-09-25 dump every extrinsics row's camera
        # is its dive's).
        """
        SELECT l.number AS id, l.laser_position::json AS laser_position,
               l.laser_axis::json AS laser_axis, l.created_at,
               d.number AS dive_id, dev.number AS camera_id
        FROM public.laser_calibrations l
        JOIN public.dives d ON d.tenant_id = l.tenant_id AND d.id = l.dive_id
        LEFT JOIN public.devices dev
          ON dev.tenant_id = d.tenant_id AND dev.id = d.device_id
        WHERE l.outcome = 'accepted'
          AND NOT EXISTS (
              SELECT 1 FROM public.laser_calibrations later
              WHERE later.tenant_id = l.tenant_id AND later.dive_id = l.dive_id
                AND later.outcome = 'accepted' AND later.seq > l.seq
          )
        """,
    ),
    (
        "divelaserline",
        f"""
        SELECT l.number AS id, d.number AS dive_id, l.a, l.b, l.c, l.n_points,
               l.inlier_count, l.inlier_fraction, l.residual_std, l.label_noise_mad,
               l.line_confidence, l.fitted_at
        FROM public.dive_laser_lines l
        JOIN public.dives d ON d.tenant_id = l.tenant_id AND d.id = l.dive_id
        WHERE {_CURRENT.format(t="l", view="current_dive_laser_lines")}
        """,
    ),
    (
        "laserlabel",
        f"""
        SELECT l.number AS id, l.ls_task_id AS label_studio_task_id, l.x, l.y,
               l.label, c.number AS image_id, l.ls_updated_at AS updated_at,
               l.completed, l.ls_payload::json AS label_studio_json,
               l.ls_project_id AS label_studio_project_id, l.superseded,
               l.needs_reprocess, l.superseded_reason
        FROM public.laser_labels l
        {_CAPTURE.format(t="l")}
        """,
    ),
    (
        "headtaillabel",
        f"""
        SELECT l.number AS id, l.ls_task_id AS label_studio_task_id, l.head_x,
               l.head_y, l.tail_x, l.tail_y, c.number AS image_id,
               l.ls_updated_at AS updated_at, l.completed,
               l.ls_payload::json AS label_studio_json,
               l.ls_project_id AS label_studio_project_id, l.superseded,
               l.needs_reprocess
        FROM public.head_tail_labels l
        {_CAPTURE.format(t="l")}
        """,
    ),
    (
        "specieslabel",
        f"""
        SELECT l.number AS id, l.ls_task_id AS label_studio_task_id,
               l.ls_updated_at AS updated_at, l.completed,
               l.ls_payload::json AS label_studio_json, c.number AS image_id,
               l.image_url, l.ls_project_id AS label_studio_project_id,
               l.top_three_photos_of_group, l.content_of_image,
               l.fish_measurable_category, l.fish_angle_category,
               l.fish_curved_category, l."grouping", l.superseded,
               l.needs_reprocess, l.fish_angle_degrees
        FROM public.species_labels l
        {_CAPTURE.format(t="l")}
        """,
    ),
    (
        "diveslatelabel",
        f"""
        SELECT l.number AS id, l.ls_task_id AS label_studio_task_id,
               l.ls_project_id AS label_studio_project_id, l.image_url,
               l.ls_updated_at AS updated_at, l.completed,
               l.ls_payload::json AS label_studio_json, c.number AS image_id,
               l.upside_down, l.reference_points::json AS reference_points,
               l.slate_rectangle::json AS slate_rectangle,
               l.skipped_points::json AS skipped_points, l.superseded,
               l.needs_reprocess
        FROM public.slate_labels l
        {_CAPTURE.format(t="l")}
        """,
    ),
    (
        "laserprediction",
        # With the gate's latest verdict (0021), as v1 updated it in place.
        f"""
        SELECT p.number AS id, p.x, p.y, p.confidence, p.width, p.height,
               p.created_at, c.number AS image_id, p.color, p.predictor_version,
               p.checkpoint, p.core_version, p.color_margin,
               p.rejected_out_of_region, p.auto_accept, p.gate_verdict,
               p.line_offset_px, p.line_position_z
        FROM public.current_laser_predictions_gated p
        {_CAPTURE.format(t="p")}
        """,
    ),
    (
        "laserdepth",
        f"""
        SELECT d.number AS id, d.depth_m, d.range_m, d.residual_m, d.created_at,
               c.number AS image_id, ll.number AS laser_label_id,
               lc.number AS laser_extrinsics_id
        FROM public.laser_depths d
        {_CAPTURE.format(t="d")}
        LEFT JOIN public.laser_labels ll
          ON ll.tenant_id = d.tenant_id AND ll.id = d.laser_label_id
        LEFT JOIN public.laser_calibrations lc
          ON lc.tenant_id = d.tenant_id AND lc.id = d.laser_calibration_id
        WHERE {_CURRENT.format(t="d", view="current_laser_depths")}
        """,
    ),
    (
        "measurement",
        # No server measurement of the capture is newer. A migrated row with
        # no capture (v1's image_id was nullable) has no newer one: it stands.
        """
        SELECT m.number AS id, m.length_m, c.number AS image_id,
               f.number AS fish_id, lc.number AS laser_extrinsics_id
        FROM public.measurements m
        LEFT JOIN public.captures c
          ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
        LEFT JOIN public.fish f ON f.tenant_id = m.tenant_id AND f.id = m.fish_id
        LEFT JOIN public.laser_calibrations lc
          ON lc.tenant_id = m.tenant_id AND lc.id = m.laser_calibration_id
        WHERE m.source = 'server'
          AND NOT EXISTS (
              SELECT 1 FROM public.measurements later
              WHERE later.tenant_id = m.tenant_id
                AND later.capture_id = m.capture_id
                AND later.source = 'server' AND later.seq > m.seq
          )
        """,
    ),
    (
        "diveframecluster",
        """
        SELECT k.number AS id, d.number AS dive_id,
               upper(k.formed_by) AS data_source, k.updated_at,
               f.number AS fish_id
        FROM public.dive_frame_clusters k
        LEFT JOIN public.dives d ON d.tenant_id = k.tenant_id AND d.id = k.dive_id
        LEFT JOIN public.fish f ON f.tenant_id = k.tenant_id AND f.id = k.fish_id
        """,
    ),
    (
        "diveframeclusterimagemapping",
        f"""
        SELECT k.number AS dive_frame_cluster_id, c.number AS image_id
        FROM public.dive_frame_cluster_captures m
        JOIN public.dive_frame_clusters k
          ON k.tenant_id = m.tenant_id AND k.id = m.cluster_id
        {_CAPTURE.format(t="m")}
        """,
    ),
)

# --- v1's fish views, verbatim but for the schema -------------------------------

_MISLABEL_MIN_OWN_PCT_ERROR = 15.0
_MISLABEL_MAX_OTHER_PCT_ERROR = 10.0

FISH_VIEWS: tuple[tuple[str, str], ...] = (
    (
        "fish_model_measurement_accuracy",
        # views.py:676-693. Wild fish (no model name) and models without a
        # reference drop out through the inner joins, as in v1.
        """
        SELECT
            m.id            AS measurement_id,
            m.image_id      AS image_id,
            i.dive_id       AS dive_id,
            f.id            AS fish_id,
            f.name          AS model_name,
            r.known_length_m AS known_length_m,
            m.length_m      AS length_m,
            (m.length_m - r.known_length_m) AS error_m,
            (100.0 * (m.length_m - r.known_length_m) / r.known_length_m) AS pct_error
        FROM v1.measurement m
        JOIN v1.image i ON i.id = m.image_id
        JOIN v1.fish f ON f.id = m.fish_id
        JOIN v1.fishmodelreference r ON r.name = f.name
        WHERE m.length_m IS NOT NULL
        """,
    ),
    (
        "fish_length_estimate",
        # views.py:734-765: nearest rank, (9n+9)/10 = ceil(0.9n) and
        # (n+1)/2 = ceil(0.5n) in integer arithmetic.
        """
        WITH ranked AS (
            SELECT
                m.length_m   AS length_m,
                i.dive_id    AS dive_id,
                f.id         AS fish_id,
                f.name       AS model_name,
                f.species_id AS species_id,
                ROW_NUMBER() OVER (
                    PARTITION BY f.id, i.dive_id ORDER BY m.length_m
                ) AS rn,
                COUNT(*) OVER (PARTITION BY f.id, i.dive_id) AS n
            FROM v1.measurement m
            JOIN v1.image i ON i.id = m.image_id
            JOIN v1.fish f ON f.id = m.fish_id
            WHERE m.length_m IS NOT NULL
        )
        SELECT
            fish_id,
            dive_id,
            model_name,
            species_id,
            n AS n_frames,
            MAX(CASE WHEN rn = (9 * n + 9) / 10 THEN length_m END) AS length_p90_m,
            MAX(CASE WHEN rn = (n + 1) / 2      THEN length_m END) AS length_median_m,
            MAX(length_m) AS length_max_m,
            MIN(length_m) AS length_min_m,
            AVG(length_m) AS length_mean_m
        FROM ranked
        GROUP BY fish_id, dive_id, model_name, species_id, n
        """,
    ),
    (
        "fish_model_species_mislabel_suspects",
        # views.py:828-869. Provisional lengths and calibration targets are
        # never offered as the better fit; a target is never a suspect.
        f"""
        WITH frame_fit AS (
            SELECT a.image_id,
                   r.name AS best_fit_model,
                   100.0 * (a.length_m - r.known_length_m) / r.known_length_m
                       AS best_fit_pct_error,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.image_id
                       ORDER BY ABS(a.length_m - r.known_length_m) / r.known_length_m
                   ) AS rk
            FROM public.fish_model_measurement_accuracy a
            CROSS JOIN v1.fishmodelreference r
            WHERE NOT r.is_provisional
              AND NOT {calibration_target_name_sql("r.name")}
        )
        SELECT
            a.image_id,
            a.dive_id,
            a.model_name AS labeled_model,
            a.known_length_m,
            a.length_m,
            a.pct_error,
            f.best_fit_model,
            f.best_fit_pct_error,
            CASE
                WHEN a.pct_error > {_MISLABEL_MIN_OWN_PCT_ERROR} THEN 'high'
                ELSE 'medium'
            END AS confidence
        FROM public.fish_model_measurement_accuracy a
        JOIN frame_fit f ON f.image_id = a.image_id AND f.rk = 1
        WHERE f.best_fit_model <> a.model_name
          AND NOT {calibration_target_name_sql("a.model_name")}
          AND ABS(a.pct_error) > {_MISLABEL_MIN_OWN_PCT_ERROR}
          AND ABS(f.best_fit_pct_error) < {_MISLABEL_MAX_OTHER_PCT_ERROR}
        """,
    ),
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA v1")
    op.execute(
        "COMMENT ON SCHEMA v1 IS 'v1-shaped read-only views for the research "
        "repos (migration 0031); ids are numbers'"
    )
    for name, select in V1_VIEWS:
        op.execute(f"CREATE VIEW v1.{name} WITH (security_invoker = true) AS {select}")
    for name, select in FISH_VIEWS:
        op.execute(
            f"CREATE VIEW public.{name} WITH (security_invoker = true) AS {select}"
        )


def downgrade() -> None:
    for name, _ in reversed(FISH_VIEWS):
        op.execute(f"DROP VIEW public.{name}")
    for name, _ in reversed(V1_VIEWS):
        op.execute(f"DROP VIEW v1.{name}")
    op.execute("DROP SCHEMA v1")
