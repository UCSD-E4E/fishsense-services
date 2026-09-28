"""The v1-shaped research schema reproduces v1 on migrated data.

The research repos query v1's tables by name (PLAN.md §2.7, §9.20):

* imwut_2026_fishsense_lite@5e9dd3d fish_model_analysis/sql/extract_*.sql;
* cscw-fishsense2027 sql/extract_*.sql (untracked in its working tree at
  2cb6474; copied as found);
* cscw's unscripted extracts of dive, laserextrinsics, laserlabel, image,
  diveslatelabel, diveslate, laserprediction, divelaserline, diveframecluster
  and specieslabel (docs/port-map/consumers.json).

They are copied verbatim into ``fixtures/research_sql/`` (psql meta-commands
are skipped when run here). Schema ``v1`` holds a view per v1 table with v1's
name, columns and ids (rows' ``number``s), so each of them runs unchanged with
only ``search_path = v1, public``.

The parity tests seed a small v1 corpus into v1's real schema
(``fixtures/v1_schema.sql``, which also carries v1's own fish views), run every
extract and view there, migrate it with migrate-v1, run the same SQL as a
research login against v2, and compare row by row. JSON is compared parsed:
v1 stored ``json``, v2 stores ``jsonb``, whose text is normalised (see
`test_json_text_is_normalised_but_equal`).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from research_seed import RESEARCH_PASSWORD, RESEARCH_SEARCH_PATH
from test_v1_migration import (  # noqa: F401  (fixtures)
    _run,
    server,
    v1,
    v1_template,
    v2,
    v2_template,
)

#: libpq's `options` form of the research login's search_path.
SEARCH_PATH = RESEARCH_SEARCH_PATH.replace(" ", "")
SQL_DIR = Path(__file__).parent / "fixtures" / "research_sql"
EXTRACTS = sorted(p.stem for p in SQL_DIR.glob("*.sql"))

#: v1 columns no view exposes, and why.
LEFT_OUT = {
    # v2 keeps a refusal as a laser_calibrations row, and a later accept or a
    # clear retires it; v1's three columns are not reproduced. No research
    # query reads them.
    "dive": {
        "calibration_refused_at",
        "calibration_refused_reason",
        "calibration_refused_labels_at",
    },
    # v1's `user` table is not migrated: a label's labeler is its Label Studio
    # id (`ls_labeler_id`), not a v1 user id. The annotation JSON still names
    # `completed_by`, which is what cscw reads.
    "laserlabel": {"user_id"},
    "headtaillabel": {"user_id"},
    "diveslatelabel": {"user_id"},
    # Not migrated (v1_migration.LABEL_TABLES); no research query reads them.
    "specieslabel": {
        "user_id",
        "laser_x",
        "laser_y",
        "laser_label",
        "slate_upside_down",
    },
}
#: v1 tables with no view: no research query or pubfig reads them.
TABLES_LEFT_OUT = {
    "alembic_version",
    "headtailprediction",
    "slateprediction",
    "labelstudiosynccursor",
    "user",
}
#: Columns a view adds beyond the fixture's v1 schema: v1 gained them after the
#: 2026-09-25 dump the fixture is (fishsense-lite #932, `superseded_reason`).
ADDED = {"laserlabel": {"superseded_reason"}}
#: v1's JSON columns: still `json`, so `->>`, `::jsonb` and `::text` behave.
JSON_COLUMNS = {
    ("cameraintrinsics", "camera_matrix"),
    ("cameraintrinsics", "distortion_coefficients"),
    ("laserextrinsics", "laser_position"),
    ("laserextrinsics", "laser_axis"),
    ("diveslate", "reference_points"),
    ("laserlabel", "label_studio_json"),
    ("headtaillabel", "label_studio_json"),
    ("specieslabel", "label_studio_json"),
    ("diveslatelabel", "label_studio_json"),
    ("diveslatelabel", "reference_points"),
    ("diveslatelabel", "slate_rectangle"),
    ("diveslatelabel", "skipped_points"),
}
FISH_VIEWS = {
    "fish_model_measurement_accuracy": "measurement_id",
    "fish_length_estimate": "fish_id, dive_id",
    "fish_model_species_mislabel_suspects": "image_id",
}


def _annotations(*created_at: str, cancelled: bool = False) -> str:
    """A Label Studio task payload, as v1's label_studio_json kept it."""
    return json.dumps(
        {
            "id": 1,
            "annotations": [
                {
                    "id": 700 + i,
                    "completed_by": 3,
                    "was_cancelled": cancelled,
                    "lead_time": 4.25,
                    "created_at": at,
                    "updated_at": at,
                    "parent_prediction": 11,
                    "parent_annotation": None,
                    "last_action": "prediction_changed",
                    "ground_truth": False,
                    "result": [
                        {
                            "type": "keypointlabels",
                            "value": {"x": 50.1, "y": 60.2, "keypointlabels": ["Red"]},
                            "original_width": 4000,
                            "original_height": 3000,
                            "origin": "prediction-changed",
                        }
                    ],
                }
                for i, at in enumerate(created_at)
            ],
        },
        separators=(",", ":"),
    )


#: v1's corpus: dives the research SQL hard-codes (5 stereo, 58 head/tail, 87
#: angles, 279 field), dive 32 which 5 borrows from, and dive 12 whose
#: extrinsics a later refusal did not remove. JSON written compactly, as v1's
#: API did, so its text differs from jsonb's.
V1_CORPUS = r"""
INSERT INTO calibrationtarget (id, name, rows, cols, square_size_m, notes, created_at)
VALUES (1, 'E4E Checkerboard', 10, 14, 0.04217, NULL, '2026-01-01');
INSERT INTO fishmodelreference (id, name, known_length_m, notes, is_provisional)
VALUES (1, 'Snook', 0.455, NULL, false), (2, 'Grouper', 0.36, NULL, false),
       (3, 'Ruler', 0.3429, '0.5 to 14 in', false),
       (4, 'Weasly Fish', 0.313, 'fork length', true);
INSERT INTO species (id, scientific_name, common_name)
VALUES (1, 'Lachnolaimus maximus', 'Hogfish'), (2, 'Mycteroperca microlepis', 'Gag');
INSERT INTO diveslate (id, name, path, created_at, dpi, reference_points)
VALUES (1, 'H-Slate', 'slates/h.pdf', '2026-01-01', 300, '[[1,2],[3,4]]');
INSERT INTO camera (id, serial_number, name)
VALUES (1, 'BHK001', 'FSL-01'), (2, 'BHK002', 'FSL-02');
INSERT INTO cameraintrinsics (id, camera_matrix, distortion_coefficients, camera_id)
VALUES (1, '[[2800,0,2000],[0,2800,1500],[0,0,1]]', '[0.1,-0.2,0,0,0]', 1),
       (2, '[[2810.5,0,2001],[0,2811,1499],[0,0,1]]', '[0.05,-0.1,0,0,0]', 2);
INSERT INTO dive (id, path, dive_datetime, priority, camera_id, dive_slate_id,
                  flip_dive_slate, name, calibration_dive_id, notes,
                  calibration_target_id)
VALUES (32, 'dives/32', '2023-08-03 09:11+00', 'LOW', 1, 1, false,
        'H Slate Dive 1', NULL, NULL, NULL),
       (5, 'dives/5', '2023-08-03 08:00+00', 'HIGH', 1, NULL, false,
        'Hogfish01_MolHITW_0926_080323', NULL, NULL, NULL),
       (58, 'dives/58', '2023-08-14 10:00+00', 'HIGH', 2, NULL, false,
        'Models 58', NULL, 'checkerboard day', 1),
       (87, 'dives/87', '2023-08-20 10:00+00', 'LOW', 2, NULL, false,
        'Snook angles', NULL, NULL, NULL),
       (279, 'dives/279', '2025-03-06 17:00+00', 'HIGH', 1, 1, false,
        'Reef 279', NULL, NULL, NULL),
       (12, 'dives/12', '2025-04-01 12:00+00', 'NONE', 2, NULL, true,
        'refused later', NULL, NULL, NULL);
UPDATE dive SET calibration_dive_id = 32 WHERE id = 5;
UPDATE dive SET calibration_dive_id = 58 WHERE id = 87;
INSERT INTO laserextrinsics (id, laser_position, laser_axis, created_at, dive_id,
                             camera_id)
VALUES (1, '[0.10551,0,0]', '[0,0,1]', '2026-09-16', 32, 1),
       (2, '[0.1,0.01,0]', '[0.001,0,1]', '2026-08-01', 58, 2),
       (3, '[0.11,0,0]', '[0,0.002,1]', '2026-09-01', 279, 1),
       (4, '[0.099,0,0]', '[0,0,1]', '2026-03-01', 12, 2);
UPDATE dive SET calibration_refused_at = '2026-04-01',
    calibration_refused_reason = 'baseline implausible',
    calibration_refused_labels_at = '2026-03-31'
WHERE id = 12;
INSERT INTO divelaserline (id, dive_id, a, b, c, n_points, inlier_count,
    inlier_fraction, residual_std, label_noise_mad, line_confidence, fitted_at)
VALUES (1, 58, 0.6, 0.8, -1200, 40, 37, 0.925, 1.4, 0.9, 270256.98, '2026-08-02');
INSERT INTO image (id, path, taken_datetime, checksum, is_canonical, dive_id, camera_id)
VALUES (320, 'dives/32/P320.ORF', '2023-08-03 09:12+00', md5('320'), true, 32, 1),
       (500, 'dives/5/P500.ORF', '2023-08-03 08:01+00', md5('500'), true, 5, 1),
       (501, 'dives/5/P501.ORF', '2023-08-03 08:02+00', md5('501'), true, 5, 1),
       (580, 'dives/58/P580.ORF', '2023-08-14 10:01+00', md5('580'), true, 58, 2),
       (581, 'dives/58/P581.ORF', '2023-08-14 10:02+00', md5('581'), true, 58, 2),
       (582, 'dives/58/P582.ORF', '2023-08-14 10:03+00', md5('582'), true, 58, 2),
       (583, 'dives/12/P580.ORF', '2023-08-14 10:01+00', md5('580'), false, 12, 2),
       (870, 'dives/87/P870.ORF', '2023-08-20 10:01+00', md5('870'), true, 87, 2),
       (871, 'dives/87/P871.ORF', '2023-08-20 10:02+00', md5('871'), true, 87, 2),
       (2790, 'dives/279/P2790.ORF', '2025-03-06 17:01+00', md5('2790'), true, 279, 1),
       (2791, 'dives/279/P2791.ORF', '2025-03-06 17:02+00', md5('2791'), true, 279, 1);
INSERT INTO fish (id, species_id, name)
VALUES (1, NULL, 'Snook'), (2, NULL, 'Grouper'), (3, NULL, 'Ruler'),
       (4, 1, NULL), (5, 2, NULL);
INSERT INTO diveframecluster (id, dive_id, data_source, updated_at, fish_id)
VALUES (1, 279, 'LABEL_STUDIO', '2025-03-07', 5), (2, 279, 'PREDICTION', NULL, NULL),
       (3, 5, 'LABEL_STUDIO', '2023-08-04', 4);
INSERT INTO diveframeclusterimagemapping (dive_frame_cluster_id, image_id)
VALUES (1, 2790), (1, 2791), (2, 2790), (3, 500), (3, 501);
INSERT INTO laserprediction (id, x, y, confidence, width, height, created_at,
    image_id, color, predictor_version, checkpoint, core_version, color_margin,
    rejected_out_of_region, auto_accept, gate_verdict, line_offset_px,
    line_position_z)
VALUES (1, 2010.5, 1490.25, 0.93, 4000, 3000, '2026-08-02', 580, 'red', 3,
        'laser-v3.pt', '4.0.0', 0.4, false, true, 'auto_accepted', 1.2, 2.5),
       (2, NULL, NULL, 0.1, 4000, 3000, '2026-08-02', 581, NULL, 3,
        'laser-v3.pt', '4.0.0', NULL, false, false, 'no_prediction', NULL, NULL);
"""


def _labels(conn) -> None:
    """Every measured frame's labels, with the Label Studio payloads cscw reads."""
    species = [
        # id, image, content, top3, completed, superseded, angle, angle cat
        (1, 580, "Fish Model, Grouper", True, True, True, None, None),
        (2, 580, "Fish Model, Snook", True, True, False, None, None),
        (3, 581, "Calibration Targets, Ruler", True, True, False, None, None),
        (4, 582, "Fish Model, Grouper", True, True, False, None, None),
        (5, 870, "Fish Model, Snook", True, True, False, 30.0, "Angled"),
        (6, 871, "Fish Model, Snook", False, True, False, 0.0, "Straight"),
        (7, 500, "Fish, Hogfish (Lachnolaimus maximus)", True, True, False, None, None),
        (8, 501, "Fish, Hogfish (Lachnolaimus maximus)", False, True, False, None, None),
        (9, 2790, "Fish, Gag (Mycteroperca microlepis)", True, True, False, None, None),
        (10, 2791, "Fish, Gag (Mycteroperca microlepis)", True, False, False, None, None),
        (11, 320, "Slate, Laser on slate", False, True, False, None, None),
    ]  # fmt: skip
    for sid, image, content, top3, done, gone, angle, angle_cat in species:
        conn.execute(
            text(
                "INSERT INTO specieslabel (id, label_studio_task_id, updated_at, "
                "completed, label_studio_json, image_id, image_url, "
                "label_studio_project_id, top_three_photos_of_group, "
                "slate_upside_down, laser_x, laser_y, laser_label, content_of_image, "
                "fish_measurable_category, fish_angle_category, "
                "fish_curved_category, grouping, superseded, needs_reprocess, "
                "fish_angle_degrees) VALUES (:id, :task, '2026-08-05', :done, "
                ":payload, :image, :url, :project, :top3, false, 1.0, 2.0, 'Red', "
                ":content, 'Measurable', :angle_cat, 'Straight', 'Group 1', :gone, "
                "false, :angle)"
            ),
            {
                "id": sid,
                "task": 4000 + sid,
                "project": 40 + sid,  # one per label: v1 keys (image, project)
                "done": done,
                "payload": _annotations("2025-03-07T10:00:00.000001Z"),
                "image": image,
                "url": f"s3://fishsense/{image}.jpg",
                "top3": top3,
                "content": content,
                "angle_cat": angle_cat,
                "gone": gone,
                "angle": angle,
            },
        )
    laser = [
        # id, image, x, y, superseded, annotations' created_at
        (1, 500, 2000.0, 1500.0, False, ("2023-08-10T00:00:00Z",)),
        (2, 501, 2001.0, 1501.0, False, ()),
        (3, 580, 1990.0, 1480.0, True, ("2026-01-02T00:00:00Z",)),
        (4, 580, 2010.0, 1490.0, False, ("2026-08-03T00:00:00Z",
                                         "2026-08-01T00:00:00Z")),
        (5, 580, 2011.0, 1491.0, False, ()),
        (6, 581, 2020.0, 1495.0, False, ("2026-08-03T01:00:00Z",)),
        (7, 582, 2030.0, 1496.0, False, ()),
        (8, 870, 2040.0, 1497.0, False, ("2026-08-21T00:00:00Z",)),
        (9, 871, 2050.0, 1498.0, False, ()),
        (10, 2790, 2060.0, 1499.0, False, ("2025-03-08T00:00:00Z",)),
        (11, 2791, 2070.0, 1500.0, False, ("2025-03-09T00:00:00Z",)),
    ]  # fmt: skip
    for lid, image, x, y, gone, created in laser:
        conn.execute(
            text(
                "INSERT INTO laserlabel (id, label_studio_task_id, x, y, label, "
                "image_id, updated_at, completed, label_studio_json, "
                "label_studio_project_id, superseded, needs_reprocess) VALUES (:id, "
                ":task, :x, :y, 'Red', :image, '2026-08-05', true, :payload, :project, "
                ":gone, false)"
            ),
            {
                "id": lid,
                "task": 5000 + lid,
                "project": 60 + lid,
                "x": x,
                "y": y,
                "image": image,
                "payload": _annotations(*created) if created else None,
                "gone": gone,
            },
        )
    head_tail = [
        # id, image, head x, completed, superseded
        (1, 500, 1000.0, True, False),
        (2, 580, 1100.0, True, False),
        (3, 580, 1105.0, True, True),
        (4, 581, 1200.0, True, False),
        (5, 582, 1300.0, True, False),
        (6, 870, 1400.0, False, False),
        (7, 2790, None, True, False),
    ]
    for hid, image, head_x, done, gone in head_tail:
        conn.execute(
            text(
                "INSERT INTO headtaillabel (id, label_studio_task_id, head_x, head_y, "
                "tail_x, tail_y, image_id, updated_at, completed, label_studio_json, "
                "label_studio_project_id, superseded, needs_reprocess) VALUES (:id, "
                ":task, :hx, 1500.0, :tx, 1510.0, :image, '2026-08-06', :done, "
                ":payload, :project, :gone, false)"
            ),
            {
                "id": hid,
                "task": 6000 + hid,
                "project": 80 + hid,
                "hx": head_x,
                "tx": None if head_x is None else head_x + 600.5,
                "image": image,
                "done": done,
                "payload": _annotations("2026-08-06T00:00:00Z", cancelled=hid == 3),
                "gone": gone,
            },
        )
    conn.execute(text("""
        INSERT INTO diveslatelabel (id, label_studio_task_id, label_studio_project_id,
            image_url, updated_at, completed, label_studio_json, image_id,
            upside_down, reference_points, slate_rectangle, skipped_points,
            superseded, needs_reprocess)
        VALUES (1, 91, 30, 's3://fishsense/320.jpg', '2026-09-15', true, NULL, 320,
                false, '[[10,20],[30,40]]', '[[0,0],[1,1]]', '[]', false, false)
        """))


def _results(conn) -> None:
    conn.execute(text("""
        INSERT INTO laserdepth (id, depth_m, range_m, residual_m, created_at, image_id,
                                laser_label_id, laser_extrinsics_id)
        VALUES (1, 2.1, 2.3, 0.01, '2026-09-16', 500, 1, 1),
               (2, 1.5, 1.6, 0.02, '2026-08-02', 580, 4, 2),
               (3, 1.7, NULL, NULL, '2026-08-02', 581, 6, 2),
               (4, 1.8, 1.9, 0.0, '2026-08-02', 582, 7, 2),
               (5, 2.5, 2.6, 0.01, '2026-08-22', 870, 8, 2),
               (6, 6.1, 6.4, 0.03, '2026-09-02', 2790, 10, 3);
        INSERT INTO measurement (id, length_m, image_id, fish_id, laser_extrinsics_id)
        VALUES (1, 0.41, 500, 4, 1), (2, 0.40, 501, 4, 1),
               (3, 0.44, 580, 1, 2), (4, 0.35, 581, 3, 2),
               (5, 0.47, 582, 2, 2), (6, 0.40, 870, 1, 2),
               (7, 0.43, 871, 1, 2), (8, 0.52, 2790, 5, 3),
               (9, 0.50, 2791, 5, 3);
        """))


def _seed_corpus(v1: Engine) -> None:
    with v1.begin() as conn:
        conn.execute(text(V1_CORPUS))
        _labels(conn)
        _results(conn)


@pytest.fixture
def migrated(v1, v2, research_login) -> tuple[Engine, Engine]:
    """(v1 with the corpus, v2 migrated from it and read as research)."""
    _seed_corpus(v1)
    _run(v1, v2)
    research = create_engine(
        v2.url.set(username=research_login, password=RESEARCH_PASSWORD),
        connect_args={"options": f"-c search_path={SEARCH_PATH}"},
    )
    yield v1, research
    research.dispose()


def _extract(name: str) -> str:
    """The research SQL as the psql script runs it, minus psql meta-commands."""
    sql = (SQL_DIR / f"{name}.sql").read_text()
    return "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))


def _normal(value):
    """Compare JSON by value (v1 `json` text vs v2 `jsonb` text)."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except ValueError:
            return value
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _query(engine: Engine, sql: str) -> tuple[list[str], list[tuple]]:
    """(column names, rows in a canonical order). Through the raw driver with
    no parameters, so the SQL's `%` wildcards reach Postgres as written."""
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(sql)
        names = [c.name for c in cursor.description]
        found = [tuple(map(_normal, r)) for r in cursor.fetchall()]
    finally:
        raw.close()
    # Two extracts (cscw's) have no ORDER BY: compare every result as a
    # multiset of rows.
    return names, sorted(found, key=lambda r: [(v is None, str(v)) for v in r])


# --- the research SQL, unchanged ---------------------------------------------------


@pytest.mark.parametrize("extract", EXTRACTS)
def test_each_research_extract_returns_v1s_rows(migrated, extract):
    v1, research = migrated

    in_v1 = _query(v1, _extract(extract))
    in_v2 = _query(research, _extract(extract))

    assert in_v1[1], f"the corpus exercises {extract}"
    assert in_v2 == in_v1


@pytest.mark.parametrize("view", sorted(FISH_VIEWS))
def test_each_fish_view_returns_v1s_rows(migrated, view):
    v1, research = migrated
    sql = f"SELECT * FROM {view} ORDER BY {FISH_VIEWS[view]}"

    in_v1 = _query(v1, sql)
    in_v2 = _query(research, sql)

    assert in_v1[1], f"the corpus exercises {view}"
    assert in_v2 == in_v1


# --- every v1 table, column by column ----------------------------------------------


def _columns(engine: Engine, schema: str) -> dict[str, dict[str, str]]:
    with engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema = :s "
                "ORDER BY table_name, ordinal_position"
            ),
            {"s": schema},
        )
        columns: dict[str, dict[str, str]] = {}
        for table, column, data_type in found:
            columns.setdefault(table, {})[column] = data_type
        return columns


def test_every_v1_table_has_a_view_with_its_columns(migrated):
    v1, research = migrated
    v1_tables = _columns(v1, "public")
    views = _columns(research, "v1")

    for table in set(v1_tables) - set(views) - TABLES_LEFT_OUT:
        if table in FISH_VIEWS or table == "dive_pipeline_status":
            continue  # v1's views, not tables
        pytest.fail(f"no view for v1 table {table}")
    for table, columns in views.items():
        expected = set(v1_tables[table]) - LEFT_OUT.get(table, set())
        assert set(columns) == expected | ADDED.get(table, set()), table
        for column, data_type in columns.items():
            if (table, column) in JSON_COLUMNS:
                assert data_type == "json", (table, column)


def test_every_v1_table_view_returns_v1s_rows(migrated):
    """Each view, over the columns it shares with v1's table, row for row."""
    v1, research = migrated

    compared = 0
    for table, columns in _columns(research, "v1").items():
        added = ADDED.get(table, set())
        shared = ", ".join(f'"{c}"' for c in columns if c not in added)
        sql = f'SELECT {shared} FROM "{table}"'
        in_v1, in_v2 = _query(v1, sql), _query(research, sql)
        assert in_v2 == in_v1, table
        compared += len(in_v1[1])

    assert compared > 50


# --- the differences, each for a reason --------------------------------------------


def test_json_text_is_normalised_but_equal(migrated):
    """v1 stored `json` verbatim; v2 stores `jsonb`, whose text has its own
    spacing and key order. Research exports that pin `::text` byte for byte
    (imwut's pos/ax/km/dist columns) change text, not value."""
    v1, research = migrated
    sql = "SELECT laser_position::text FROM laserextrinsics WHERE id = 1"

    with v1.connect() as conn:
        v1_text = conn.execute(text(sql)).scalar_one()
    with research.connect() as conn:
        v2_text = conn.execute(text(sql)).scalar_one()

    assert (v1_text, v2_text) == ("[0.10551,0,0]", "[0.10551, 0, 0]")


def test_extrinsics_survive_a_later_refusal_as_in_v1(migrated):
    """v1 kept a dive's extrinsics row when a later fit was refused; the
    calibration fits extract reads accepted fits. v2's current calibration of
    dive 12 is the refusal, but its accepted row is still v1's."""
    _, research = migrated

    _, got = _query(research, "SELECT id, dive_id FROM laserextrinsics ORDER BY id")

    assert (4, 12) in got


def test_v1s_nulls_arrive_as_v2s_defaults(v1, v2, research_login):
    """migrate-v1 reads a v1 NULL as v1's ORM did (docs: v1_migration): a NULL
    priority is LOW, a NULL flip or completed is false, a NULL laser/head-tail
    `superseded` is not live and a NULL species `superseded` is. The views show
    v2's value, so v1 SQL comparing these columns can see a row v1 hid."""
    with v1.begin() as conn:
        conn.execute(text(V1_CORPUS))
        conn.execute(
            text("UPDATE dive SET priority = NULL, flip_dive_slate = NULL WHERE id = 5")
        )
        _labels(conn)
        conn.execute(text("UPDATE laserlabel SET superseded = NULL WHERE id = 2"))
        conn.execute(
            text(
                "UPDATE specieslabel SET superseded = NULL, completed = NULL "
                "WHERE id = 8"
            )
        )
    _run(v1, v2)
    research = create_engine(
        v2.url.set(username=research_login, password=RESEARCH_PASSWORD),
        connect_args={"options": f"-c search_path={SEARCH_PATH}"},
    )
    try:
        got = [
            _query(research, sql)[1]
            for sql in (
                "SELECT priority, flip_dive_slate FROM dive WHERE id = 5",
                "SELECT superseded FROM laserlabel WHERE id = 2",
                "SELECT superseded, completed FROM specieslabel WHERE id = 8",
            )
        ]
    finally:
        research.dispose()

    assert got == [[("LOW", False)], [(True,)], [(False, False)]]
