"""The one-shot v1 -> v2 data migration (PLAN.md §6.4).

Reads v1's ``fishsense`` database and writes the v2 database as the **lab
tenant**, in dependency order, inside **one transaction**: it lands whole or not
at all. Every migrated row keeps its ``v1_id``, which makes the job idempotent
(re-running inserts nothing new) and keeps v1 ids addressable for the research
repos.

It never invents data. Values v1 never recorded stay NULL / unknown (v2's
constraints allow that only for rows with a ``v1_id``). The returned
:class:`Report` accounts for every v1 row: (rows in v1, rows migrated to v2).

v1's production data is only ever used in local rehearsals, never in tests.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, create_engine, text

BATCH = 5_000


@dataclass
class Report(Mapping):
    """Per v1 table: (rows in v1, rows migrated into v2)."""

    counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    def __getitem__(self, table: str) -> tuple[int, int]:
        return self.counts[table]

    def __iter__(self) -> Iterator[str]:
        return iter(self.counts)

    def __len__(self) -> int:
        return len(self.counts)

    def discrepancies(self) -> dict[str, tuple[int, int]]:
        return {t: c for t, c in self.counts.items() if c[0] != c[1]}


def migrate_v1(
    *,
    source_url: str,
    target_url: str,
    tenant_slug: str = "lab",
    tenant_name: str = "E4E FishSense lab",
) -> Report:
    source, target = create_engine(source_url), create_engine(target_url)
    report = Report()
    try:
        with source.connect() as v1, target.begin() as v2:
            tenant = _ensure_tenant(v2, tenant_slug, tenant_name)
            for step in STEPS:
                step(v1, v2, tenant, report)
    finally:
        source.dispose()
        target.dispose()
    return report


# --- helpers ---------------------------------------------------------------------


def _ensure_tenant(v2: Connection, slug: str, name: str):
    v2.execute(
        text(
            "INSERT INTO tenants (slug, name) VALUES (:s, :n) "
            "ON CONFLICT (slug) DO NOTHING"
        ),
        {"s": slug, "n": name},
    )
    return v2.execute(
        text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
    ).scalar_one()


def _rows(v1: Connection, sql: str) -> Iterator[dict]:
    result = v1.execution_options(stream_results=True).execute(text(sql))
    for row in result.mappings():
        yield dict(row)


def _insert(v2: Connection, sql: str, rows: Iterator[dict]) -> None:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) == BATCH:
            v2.execute(text(sql), batch)
            batch = []
    if batch:
        v2.execute(text(sql), batch)


def _ids(v2: Connection, table: str) -> dict[int, object]:
    """v1 id -> v2 id for one table's migrated rows."""
    result = v2.execute(text(f"SELECT v1_id, id FROM {table} WHERE v1_id IS NOT NULL"))
    return {v1_id: v2_id for v1_id, v2_id in result}


def _account(v1: Connection, v2: Connection, report, v1_table: str, v2_table: str):
    in_v1 = v1.execute(text(f'SELECT count(*) FROM "{v1_table}"')).scalar_one()
    in_v2 = v2.execute(
        text(f"SELECT count(*) FROM {v2_table} WHERE v1_id IS NOT NULL")
    ).scalar_one()
    report.counts[v1_table] = (in_v1, in_v2)


# --- steps, in dependency order ---------------------------------------------------


def _reference_data(v1, v2, tenant, report) -> None:
    _insert(
        v2,
        "INSERT INTO calibration_targets (name, interior_rows, interior_cols, "
        "pitch_x_m, pitch_y_m, notes, valid_from, v1_id) "
        "VALUES (:name, :rows, :cols, :square_size_m, :square_size_m, :notes, "
        "coalesce(:created_at, now()), :id) ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM calibrationtarget"),
    )
    _account(v1, v2, report, "calibrationtarget", "calibration_targets")

    # A fish model is an identity (fish_models); its known length is versioned.
    _insert(
        v2,
        "INSERT INTO fish_models (name) VALUES (:name) ON CONFLICT DO NOTHING",
        _rows(
            v1,
            "SELECT name FROM fishmodelreference "
            "UNION SELECT name FROM fish WHERE name IS NOT NULL",
        ),
    )
    _insert(
        v2,
        "INSERT INTO fish_model_references (name, known_length_m, is_provisional, "
        "notes, v1_id) VALUES (:name, :known_length_m, :is_provisional, :notes, :id) "
        "ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM fishmodelreference"),
    )
    _account(v1, v2, report, "fishmodelreference", "fish_model_references")

    _insert(
        v2,
        "INSERT INTO species (scientific_name, common_name, v1_id) "
        "VALUES (:scientific_name, :common_name, :id) ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM species"),
    )
    _account(v1, v2, report, "species", "species")

    _insert(
        v2,
        "INSERT INTO slate_templates (name, dpi, source_path, reference_points, "
        "created_at, v1_id) VALUES (:name, :dpi, :path, "
        "CAST(:reference_points AS jsonb), coalesce(:created_at, now()), :id) "
        "ON CONFLICT DO NOTHING",
        _rows(
            v1, "SELECT *, reference_points::text AS reference_points FROM diveslate"
        ),
    )
    _account(v1, v2, report, "diveslate", "slate_templates")


def _devices(v1, v2, tenant, report) -> None:
    _insert(
        v2,
        "INSERT INTO devices (tenant_id, kind, serial, name, v1_id) "
        "VALUES (:tenant, 'lite', :serial_number, :name, :id) ON CONFLICT DO NOTHING",
        ({**r, "tenant": tenant} for r in _rows(v1, "SELECT * FROM camera")),
    )
    _account(v1, v2, report, "camera", "devices")


def _dives(v1, v2, tenant, report) -> None:
    devices = _ids(v2, "devices")
    slates = _ids(v2, "slate_templates")
    targets = _ids(v2, "calibration_targets")

    def rows():
        for r in _rows(v1, "SELECT * FROM dive"):
            yield {
                "tenant": tenant,
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "dived_at": r["dive_datetime"],
                # NULL at the DB level meant the ORM default, LOW.
                "priority": (r["priority"] or "LOW").lower(),
                "notes": r["notes"],
                "flip": bool(r["flip_dive_slate"]),
                "device": devices.get(r["camera_id"]),
                "slate": slates.get(r["dive_slate_id"]),
                "target": targets.get(r["calibration_target_id"]),
            }

    _insert(
        v2,
        "INSERT INTO dives (tenant_id, v1_id, name, source_path, dived_at, priority, "
        "notes, flip_dive_slate, device_id, slate_template_id, calibration_target_id) "
        "VALUES (:tenant, :id, :name, :path, :dived_at, :priority, :notes, :flip, "
        ":device, :slate, :target) ON CONFLICT DO NOTHING",
        rows(),
    )
    # Borrowing links need every dive first.
    v2.execute(
        text(
            "UPDATE dives d SET calibration_source_dive_id = src.id "
            "FROM dives src "
            "WHERE src.tenant_id = d.tenant_id AND src.v1_id = :source_v1 "
            "AND d.v1_id = :dive_v1"
        ),
        [
            {"dive_v1": r["id"], "source_v1": r["calibration_dive_id"]}
            for r in _rows(
                v1,
                "SELECT id, calibration_dive_id FROM dive "
                "WHERE calibration_dive_id IS NOT NULL",
            )
        ]
        or [{"dive_v1": None, "source_v1": None}],
    )
    _account(v1, v2, report, "dive", "dives")


def _captures(v1, v2, tenant, report) -> None:
    dives, devices = _ids(v2, "dives"), _ids(v2, "devices")
    _insert(
        v2,
        "INSERT INTO captures (tenant_id, v1_id, dive_id, device_id, source_path, "
        "captured_at, checksum, checksum_algorithm, is_canonical) "
        "VALUES (:tenant, :id, :dive, :device, :path, :taken_datetime, :checksum, "
        "'md5', :is_canonical) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "dive": dives.get(r["dive_id"]),
                "device": devices.get(r["camera_id"]),
            }
            for r in _rows(v1, "SELECT * FROM image")
        ),
    )
    _account(v1, v2, report, "image", "captures")


def _camera_calibrations(v1, v2, tenant, report) -> None:
    devices = _ids(v2, "devices")
    _insert(
        v2,
        # v1 never recorded the medium or coordinate frame: they stay unknown.
        "INSERT INTO camera_calibrations (tenant_id, v1_id, device_id, camera_model, "
        "camera_matrix, distortion_coefficients) VALUES (:tenant, :id, :device, "
        "'pinhole', CAST(:camera_matrix AS jsonb), "
        "CAST(:distortion_coefficients AS jsonb)) ON CONFLICT DO NOTHING",
        (
            {**r, "tenant": tenant, "device": devices.get(r["camera_id"])}
            for r in _rows(
                v1,
                "SELECT id, camera_id, camera_matrix::text AS camera_matrix, "
                "distortion_coefficients::text AS distortion_coefficients "
                "FROM cameraintrinsics ORDER BY id",
            )
        ),
    )
    _account(v1, v2, report, "cameraintrinsics", "camera_calibrations")


def _laser_calibrations(v1, v2, tenant, report) -> None:
    """Extrinsics become accepted rows; dive refusals become refused rows.

    All of a dive's rows are appended in time order, so ``current`` (latest
    ``seq``) matches v1's state. A producer is named only when certain: a dive
    with no calibration target can only have been calibrated from its slate.
    """
    dives = _ids(v2, "dives")
    camera_calibrations = {
        camera_id: v2_id
        for camera_id, v2_id in v2.execute(
            text(
                "SELECT d.v1_id, min(c.id::text)::uuid FROM camera_calibrations c "
                "JOIN devices d ON d.id = c.device_id WHERE c.v1_id IS NOT NULL "
                "GROUP BY d.v1_id HAVING count(*) = 1"
            )
        )
    }
    events = _rows(
        v1,
        """
        SELECT 'accepted' AS outcome, e.id AS v1_id, NULL::bigint AS refusal_dive,
               e.dive_id, e.camera_id, e.laser_position::text AS position,
               e.laser_axis::text AS axis, NULL AS reason, NULL AS inputs_as_of,
               e.created_at, d.calibration_target_id IS NULL AS slate_only
        FROM laserextrinsics e JOIN dive d ON d.id = e.dive_id
        UNION ALL
        SELECT 'refused', NULL, d.id, d.id, d.camera_id, NULL, NULL,
               d.calibration_refused_reason, d.calibration_refused_labels_at,
               d.calibration_refused_at, d.calibration_target_id IS NULL
        FROM dive d WHERE d.calibration_refused_at IS NOT NULL
        ORDER BY created_at, outcome
        """,
    )
    _insert(
        v2,
        "INSERT INTO laser_calibrations (tenant_id, v1_id, v1_refusal_dive_id, "
        "dive_id, camera_calibration_id, producer, outcome, laser_position, "
        "laser_axis, refusal_reason, inputs_as_of, created_at) VALUES (:tenant, "
        ":v1_id, :refusal_dive, :dive, :camera_calibration, :producer, :outcome, "
        "CAST(:position AS jsonb), CAST(:axis AS jsonb), :reason, :inputs_as_of, "
        "coalesce(:created_at, now())) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "dive": dives.get(r["dive_id"]),
                "camera_calibration": camera_calibrations.get(r["camera_id"]),
                "producer": "slate" if r["slate_only"] else None,
            }
            for r in events
        ),
    )
    _account(v1, v2, report, "laserextrinsics", "laser_calibrations")
    report.counts["dive refusals"] = (
        v1.execute(
            text("SELECT count(*) FROM dive WHERE calibration_refused_at IS NOT NULL")
        ).scalar_one(),
        v2.execute(
            text(
                "SELECT count(*) FROM laser_calibrations "
                "WHERE v1_refusal_dive_id IS NOT NULL"
            )
        ).scalar_one(),
    )


def _dive_laser_lines(v1, v2, tenant, report) -> None:
    dives = _ids(v2, "dives")
    _insert(
        v2,
        "INSERT INTO dive_laser_lines (tenant_id, v1_id, dive_id, a, b, c, n_points, "
        "inlier_count, inlier_fraction, residual_std, label_noise_mad, "
        "line_confidence, fitted_at) VALUES (:tenant, :id, :dive, :a, :b, :c, "
        ":n_points, :inlier_count, :inlier_fraction, :residual_std, "
        ":label_noise_mad, :line_confidence, coalesce(:fitted_at, now())) "
        "ON CONFLICT DO NOTHING",
        (
            {**r, "tenant": tenant, "dive": dives.get(r["dive_id"])}
            for r in _rows(v1, "SELECT * FROM divelaserline ORDER BY fitted_at, id")
        ),
    )
    _account(v1, v2, report, "divelaserline", "dive_laser_lines")


# v1 label table -> (v2 table, its kind-specific columns, which of them are JSON)
LABEL_TABLES = {
    "laserlabel": ("laser_labels", ["x", "y", "label"], []),
    "headtaillabel": ("head_tail_labels", ["head_x", "head_y", "tail_x", "tail_y"], []),
    "diveslatelabel": (
        "slate_labels",
        ["upside_down", "reference_points", "slate_rectangle", "skipped_points",
         "image_url"],
        ["reference_points", "slate_rectangle", "skipped_points"],
    ),
    "specieslabel": (
        "species_labels",
        ["image_url", "grouping", "top_three_photos_of_group", "content_of_image",
         "fish_measurable_category", "fish_angle_category", "fish_curved_category",
         "fish_angle_degrees"],
        [],
    ),
}  # fmt: skip


def _labels(v1, v2, tenant, report) -> None:
    """The four label kinds. A source is named only when certain: a sentinel
    (no project) carries an imported judgement; a laser label on a frame whose
    prediction the gate auto-accepted came from the gate. Labelers become their
    Label Studio user id -- v1's user emails and names are not copied."""
    captures = _ids(v2, "captures")
    for v1_table, (v2_table, columns, json_columns) in LABEL_TABLES.items():
        auto_accepted = (
            "WHEN EXISTS (SELECT 1 FROM laserprediction p "
            "WHERE p.image_id = l.image_id AND p.auto_accept) THEN 'auto_accept' "
            if v1_table == "laserlabel"
            else ""
        )
        selected = ", ".join(
            f'l."{c}"::text AS "{c}"' if c in json_columns else f'l."{c}"'
            for c in columns
        )
        values = ", ".join(
            f"CAST(:{c} AS jsonb)" if c in json_columns else f":{c}" for c in columns
        )
        quoted = ", ".join(f'"{c}"' for c in columns)
        _insert(
            v2,
            f"INSERT INTO {v2_table} (tenant_id, v1_id, capture_id, source, "
            f"ls_project_id, ls_task_id, ls_labeler_id, ls_updated_at, completed, "
            f"superseded, needs_reprocess, ls_payload, {quoted}) VALUES (:tenant, "
            f":id, :capture, :source, :label_studio_project_id, "
            f":label_studio_task_id, :labeler, :updated_at, :completed, :superseded, "
            f":needs_reprocess, CAST(:payload AS jsonb), {values}) "
            f"ON CONFLICT DO NOTHING",
            (
                {
                    **r,
                    "tenant": tenant,
                    "capture": captures.get(r["image_id"]),
                    "completed": bool(r["completed"]),
                    "superseded": bool(r["superseded"]),
                }
                for r in _rows(
                    v1,
                    f"SELECT l.id, l.image_id, l.label_studio_project_id, "
                    f"l.label_studio_task_id, l.updated_at, l.completed, "
                    f"l.superseded, l.needs_reprocess, "
                    f"l.label_studio_json::text AS payload, "
                    f"u.label_studio_id AS labeler, {selected}, "
                    f"CASE WHEN l.label_studio_project_id IS NULL THEN 'import' "
                    f"{auto_accepted}END AS source "
                    f'FROM {v1_table} l LEFT JOIN "user" u ON u.id = l.user_id '
                    f"ORDER BY l.id",
                )
            ),
        )
        _account(v1, v2, report, v1_table, v2_table)


# v1 wrote some kinds differently.
CURSOR_KINDS = {"dive_slate": "slate", "headtail": "head_tail"}


def _sync_cursors(v1, v2, tenant, report) -> None:
    _insert(
        v2,
        "INSERT INTO label_studio_sync_cursors (tenant_id, v1_id, kind, "
        "ls_project_id, last_synced_at) VALUES (:tenant, :id, :kind, "
        ":label_studio_project_id, :last_synced_at) ON CONFLICT DO NOTHING",
        (
            {**r, "tenant": tenant, "kind": CURSOR_KINDS.get(r["kind"], r["kind"])}
            for r in _rows(v1, "SELECT * FROM labelstudiosynccursor ORDER BY id")
        ),
    )
    _account(v1, v2, report, "labelstudiosynccursor", "label_studio_sync_cursors")


def _predictions(v1, v2, tenant, report) -> None:
    """One v1 prediction per image becomes one appended v2 prediction."""
    captures = _ids(v2, "captures")
    laser_labels = _ids(v2, "laser_labels")

    def with_links(rows):
        for r in rows:
            yield {
                **r,
                "tenant": tenant,
                "capture": captures.get(r["image_id"]),
                "laser_label": laser_labels.get(r.get("laser_label_id")),
            }

    common = "tenant_id, v1_id, capture_id, width, height, confidence, created_at"
    common_values = (
        ":tenant, :id, :capture, :width, :height, :confidence, "
        "coalesce(:created_at, now())"
    )
    _insert(
        v2,
        f"INSERT INTO laser_predictions ({common}, x, y, color, color_margin, "
        "rejected_out_of_region, predictor_version, checkpoint, core_version, "
        "auto_accept, gate_verdict, line_offset_px, line_position_z) "
        f"VALUES ({common_values}, :x, :y, :color, :color_margin, "
        ":rejected_out_of_region, :predictor_version, :checkpoint, :core_version, "
        ":auto_accept, :gate_verdict, :line_offset_px, :line_position_z) "
        "ON CONFLICT DO NOTHING",
        with_links(_rows(v1, "SELECT * FROM laserprediction ORDER BY id")),
    )
    _account(v1, v2, report, "laserprediction", "laser_predictions")

    _insert(
        v2,
        f"INSERT INTO slate_predictions ({common}, reference_points, "
        f"rejected_reason) VALUES ({common_values}, "
        "CAST(:reference_points AS jsonb), :rejected_reason) ON CONFLICT DO NOTHING",
        with_links(
            _rows(
                v1,
                "SELECT *, reference_points::text AS reference_points "
                "FROM slateprediction ORDER BY id",
            )
        ),
    )
    _account(v1, v2, report, "slateprediction", "slate_predictions")

    _insert(
        v2,
        f"INSERT INTO head_tail_predictions ({common}, head_x, head_y, tail_x, "
        "tail_y, mask_area_px, silhouette_ratio, crop_x, crop_y, laser_label_id, "
        "predictor_version, checkpoint, core_version, status, "
        f"rejected_low_confidence) VALUES ({common_values}, :head_x, :head_y, "
        ":tail_x, :tail_y, :mask_area_px, :silhouette_ratio, :crop_x, :crop_y, "
        ":laser_label, :predictor_version, :checkpoint, :core_version, :status, "
        ":rejected_low_confidence) ON CONFLICT DO NOTHING",
        with_links(
            # v1 head/tail predictions have no confidence; v2's NOT NULL column
            # takes its default, 0 -- the same default v1 used for the others.
            _rows(v1, "SELECT *, 0.0 AS confidence FROM headtailprediction ORDER BY id")
        ),
    )
    _account(v1, v2, report, "headtailprediction", "head_tail_predictions")


def _fish_and_clusters(v1, v2, tenant, report) -> None:
    """A named v1 fish is a fish model, now reached by key: v1 matched the
    name as a string. Membership rows have no v1 id; their key is idempotent."""
    species = _ids(v2, "species")
    models = dict(
        (name, v2_id)
        for name, v2_id in v2.execute(text("SELECT name, id FROM fish_models"))
    )
    _insert(
        v2,
        "INSERT INTO fish (tenant_id, v1_id, species_id, fish_model_id) "
        "VALUES (:tenant, :id, :species, :model) ON CONFLICT DO NOTHING",
        (
            {
                "tenant": tenant,
                "id": r["id"],
                "species": species.get(r["species_id"]),
                "model": models.get(r["name"]),
            }
            for r in _rows(v1, "SELECT * FROM fish ORDER BY id")
        ),
    )
    _account(v1, v2, report, "fish", "fish")

    dives, fish = _ids(v2, "dives"), _ids(v2, "fish")
    _insert(
        v2,
        "INSERT INTO dive_frame_clusters (tenant_id, v1_id, dive_id, formed_by, "
        "fish_id, updated_at) VALUES (:tenant, :id, :dive, :formed_by, :fish, "
        ":updated_at) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "dive": dives.get(r["dive_id"]),
                "fish": fish.get(r["fish_id"]),
                "formed_by": r["data_source"] and r["data_source"].lower(),
            }
            for r in _rows(
                v1,
                "SELECT id, dive_id, fish_id, updated_at, data_source::text "
                "AS data_source FROM diveframecluster ORDER BY id",
            )
        ),
    )
    _account(v1, v2, report, "diveframecluster", "dive_frame_clusters")

    clusters, captures = _ids(v2, "dive_frame_clusters"), _ids(v2, "captures")
    _insert(
        v2,
        "INSERT INTO dive_frame_cluster_captures (tenant_id, cluster_id, "
        "capture_id) VALUES (:tenant, :cluster, :capture) ON CONFLICT DO NOTHING",
        (
            {
                "tenant": tenant,
                "cluster": clusters.get(r["dive_frame_cluster_id"]),
                "capture": captures.get(r["image_id"]),
            }
            for r in _rows(v1, "SELECT * FROM diveframeclusterimagemapping")
        ),
    )
    report.counts["diveframeclusterimagemapping"] = (
        v1.execute(
            text("SELECT count(*) FROM diveframeclusterimagemapping")
        ).scalar_one(),
        v2.execute(
            text(
                "SELECT count(*) FROM dive_frame_cluster_captures m "
                "JOIN dive_frame_clusters k ON k.id = m.cluster_id "
                "WHERE k.v1_id IS NOT NULL"
            )
        ).scalar_one(),
    )


def _results(v1, v2, tenant, report) -> None:
    """Depths and measurements. v1 recorded which calibration a result used and
    nothing else about how it was made: algorithm, versions and input labels
    stay unknown (v2 allows that only for migrated rows)."""
    captures, fish = _ids(v2, "captures"), _ids(v2, "fish")
    laser_labels = _ids(v2, "laser_labels")
    calibrations = _ids(v2, "laser_calibrations")  # v1 laserextrinsics ids

    _insert(
        v2,
        "INSERT INTO laser_depths (tenant_id, v1_id, capture_id, laser_label_id, "
        "laser_calibration_id, depth_m, range_m, residual_m, created_at) VALUES "
        "(:tenant, :id, :capture, :laser_label, :calibration, :depth_m, :range_m, "
        ":residual_m, coalesce(:created_at, now())) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "capture": captures.get(r["image_id"]),
                "laser_label": laser_labels.get(r["laser_label_id"]),
                "calibration": calibrations.get(r["laser_extrinsics_id"]),
            }
            for r in _rows(v1, "SELECT * FROM laserdepth ORDER BY id")
        ),
    )
    _account(v1, v2, report, "laserdepth", "laser_depths")

    _insert(
        v2,
        "INSERT INTO measurements (tenant_id, v1_id, capture_id, fish_id, source, "
        "length_m, laser_calibration_id) VALUES (:tenant, :id, :capture, :fish, "
        "'server', :length_m, :calibration) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "capture": captures.get(r["image_id"]),
                "fish": fish.get(r["fish_id"]),
                "calibration": calibrations.get(r["laser_extrinsics_id"]),
            }
            for r in _rows(v1, "SELECT * FROM measurement ORDER BY id")
        ),
    )
    _account(v1, v2, report, "measurement", "measurements")


STEPS: list[Callable] = [
    _reference_data,
    _devices,
    _dives,
    _captures,
    _camera_calibrations,
    _laser_calibrations,
    _dive_laser_lines,
    _labels,
    _sync_cursors,
    _predictions,
    _fish_and_clusters,
    _results,
]
