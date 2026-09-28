"""The v1 -> v2 data migration (PLAN.md §6.4), against v1's real schema.

Each test builds a v1 database from ``fixtures/v1_schema.sql`` -- v1's exact
production schema, no data -- inserts synthetic rows, runs the job into a
freshly migrated v2 database, and checks what arrived in the lab tenant.
Real production data is only ever used in local rehearsals, never here.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from fishsense_services_api.cli import main
from fishsense_services_api.migrations import upgrade
from fishsense_services_api.v1_migration import measurement_parity, migrate_v1

V1_SCHEMA = (Path(__file__).parent / "fixtures" / "v1_schema.sql").read_text()
APP_ROLE = "fishsense_app"
MD5 = "0123456789abcdef0123456789abcdef"


def _url(owner_url: str, name: str) -> str:
    return (
        make_url(owner_url)
        .set(drivername="postgresql+psycopg", database=name)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def server(owner_url, owner_engine) -> Iterator[Engine]:
    """A sync, autocommit connection to the test server, for CREATE DATABASE.

    Databases aren't dropped: the whole test container is discarded after the
    session, and dropping them cost ~20 s.
    """
    url = make_url(owner_url).set(drivername="postgresql+psycopg")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


def _create(server: Engine, prefix: str, template: str | None = None) -> str:
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    clause = f" TEMPLATE {template}" if template else ""
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}{clause}"))
    return name


@pytest.fixture(scope="session")
def v1_template(server, owner_url) -> str:
    """v1's schema, loaded once; each test clones it."""
    name = _create(server, "v1tmpl")
    engine = create_engine(_url(owner_url, name))
    # Raw driver, no parameters: the schema contains literal % characters.
    raw = engine.raw_connection()
    try:
        raw.cursor().execute(V1_SCHEMA)
        raw.commit()
    finally:
        raw.close()
        engine.dispose()
    return name


@pytest.fixture(scope="session")
async def v2_template(server, owner_url) -> str:
    """v2 migrated to head, once; each test clones it."""
    name = _create(server, "v2tmpl")
    await upgrade(_url(owner_url, name), app_role=APP_ROLE)
    return name


@pytest.fixture
def v1(server, owner_url, v1_template) -> Iterator[Engine]:
    engine = create_engine(_url(owner_url, _create(server, "v1", v1_template)))
    yield engine
    engine.dispose()


@pytest.fixture
def v2(server, owner_url, v2_template) -> Iterator[Engine]:
    engine = create_engine(_url(owner_url, _create(server, "v2", v2_template)))
    yield engine
    engine.dispose()


def _run(v1: Engine, v2: Engine):
    return migrate_v1(
        source_url=v1.url.render_as_string(hide_password=False),
        target_url=v2.url.render_as_string(hide_password=False),
    )


def _rows(engine: Engine, sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def _seed_v1(v1: Engine) -> None:
    """A small, realistic v1: two cameras, three dives, duplicate frames."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO calibrationtarget (id, name, rows, cols, square_size_m,
                                           created_at)
            VALUES (1, 'E4E Checkerboard', 10, 14, 0.04217, now());
            INSERT INTO fishmodelreference (id, name, known_length_m, notes,
                                            is_provisional)
            VALUES (1, 'Weasly Fish', 0.313, NULL, false),
                   (2, 'Ruler', 0.3429, NULL, false);
            INSERT INTO species (id, scientific_name, common_name)
            VALUES (1, 'Lachnolaimus maximus', 'Hogfish');
            INSERT INTO diveslate (id, name, dpi, path, created_at,
                                   reference_points)
            VALUES (1, 'H-Slate', 300, 'slates/h.pdf', now(), '[[1, 2], [3, 4]]');
            INSERT INTO camera (id, serial_number, name)
            VALUES (1, 'BHK001', 'FSL-01'), (2, 'BHK002', 'FSL-02');
            INSERT INTO dive (id, name, path, dive_datetime, priority, camera_id,
                              notes, flip_dive_slate, dive_slate_id,
                              calibration_target_id, calibration_dive_id)
            VALUES (10, 'd10', 'dives/d10', now(), 'HIGH', 1, NULL, false, 1, 1,
                    NULL),
                   (11, 'd11', 'dives/d11', now(), 'NONE', 1, 'parked', NULL,
                    NULL, NULL, 10),
                   (12, 'd12', 'dives/d12', now(), 'LOW', 2, NULL, true, NULL,
                    NULL, NULL);
            """))
        conn.execute(
            text("""
                INSERT INTO image (id, path, taken_datetime, checksum, is_canonical,
                                   dive_id, camera_id)
                VALUES (100, 'dives/d10/P1.ORF', now(), :md5, true, 10, 1),
                       (101, 'dives/d12/P1.ORF', now(), :md5, false, 12, 2),
                       (102, 'dives/d11/P9.ORF', now(), :other, true, 11, 2)
                """),
            {"md5": MD5, "other": "f" * 32},
        )


def test_reference_data_arrives_versioned_and_by_key(v1, v2):
    _seed_v1(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT name, interior_rows, interior_cols, pitch_x_m, pitch_y_m, v1_id "
        "FROM calibration_targets",
    ) == [("E4E Checkerboard", 10, 14, 0.04217, 0.04217, 1)]
    assert _rows(
        v2,
        "SELECT name, known_length_m FROM current_fish_model_references ORDER BY name",
    ) == [("Ruler", 0.3429), ("Weasly Fish", 0.313)]
    assert _rows(v2, "SELECT name FROM fish_models ORDER BY name") == [
        ("Ruler",),
        ("Weasly Fish",),
    ]
    assert _rows(v2, "SELECT scientific_name, v1_id FROM species") == [
        ("Lachnolaimus maximus", 1)
    ]
    assert _rows(v2, "SELECT name, source_path, v1_id FROM slate_templates") == [
        ("H-Slate", "slates/h.pdf", 1)
    ]


def test_cameras_become_lite_devices_in_the_lab_tenant(v1, v2):
    _seed_v1(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT t.slug, d.kind, d.serial, d.name, d.v1_id FROM devices d "
        "JOIN tenants t ON t.id = d.tenant_id ORDER BY d.v1_id",
    ) == [
        ("lab", "lite", "BHK001", "FSL-01", 1),
        ("lab", "lite", "BHK002", "FSL-02", 2),
    ]


def test_dives_keep_their_state_and_borrowing(v1, v2):
    _seed_v1(v1)

    _run(v1, v2)

    dives = _rows(
        v2,
        "SELECT d.v1_id, d.source_path, d.priority, d.notes, d.flip_dive_slate, "
        "dev.v1_id, src.v1_id, st.v1_id, ct.v1_id "
        "FROM dives d JOIN devices dev ON dev.id = d.device_id "
        "LEFT JOIN dives src ON src.id = d.calibration_source_dive_id "
        "LEFT JOIN slate_templates st ON st.id = d.slate_template_id "
        "LEFT JOIN calibration_targets ct ON ct.id = d.calibration_target_id "
        "ORDER BY d.v1_id",
    )
    assert dives == [
        (10, "dives/d10", "high", None, False, 1, None, 1, 1),
        (11, "dives/d11", "none", "parked", False, 1, 10, None, None),
        (12, "dives/d12", "low", None, True, 2, None, None, None),
    ]


def test_images_become_captures_with_canonical_copies_kept(v1, v2):
    _seed_v1(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT c.v1_id, c.source_path, c.checksum, c.is_canonical, "
        "d.v1_id, dev.v1_id FROM captures c "
        "JOIN dives d ON d.id = c.dive_id JOIN devices dev ON dev.id = c.device_id "
        "ORDER BY c.v1_id",
    ) == [
        (100, "dives/d10/P1.ORF", MD5, True, 10, 1),
        (101, "dives/d12/P1.ORF", MD5, False, 12, 2),
        (102, "dives/d11/P9.ORF", "f" * 32, True, 11, 2),
    ]


def test_running_again_changes_nothing(v1, v2):
    _seed_v1(v1)
    _run(v1, v2)
    counts = "SELECT (SELECT count(*) FROM devices), (SELECT count(*) FROM dives), (SELECT count(*) FROM captures), (SELECT count(*) FROM fish_model_references)"  # noqa: E501
    before = _rows(v2, counts)

    _run(v1, v2)

    assert _rows(v2, counts) == before


def test_the_report_accounts_for_every_v1_row(v1, v2):
    _seed_v1(v1)

    report = _run(v1, v2)

    assert report["camera"] == (2, 2)
    assert report["dive"] == (3, 3)
    assert report["image"] == (3, 3)
    assert report.discrepancies() == {}


# --- cycle 2: calibrations ------------------------------------------------------


def _seed_calibrations(v1: Engine) -> None:
    """Intrinsics per camera; extrinsics on d10 (has a target) and d12 (none);
    a refusal on d12 *after* its extrinsics; a laser line on d10."""
    with v1.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO cameraintrinsics (id, camera_matrix,
                                              distortion_coefficients, camera_id)
                VALUES (1, '[[2800, 0, 2000], [0, 2800, 1500], [0, 0, 1]]', '[0.1, -0.2, 0, 0, 0]', 1),
                       (2, '[[2800, 0, 2000], [0, 2800, 1500], [0, 0, 1]]', '[0.1, -0.2, 0, 0, 0]', 2);
                INSERT INTO laserextrinsics (id, laser_position, laser_axis,
                                             created_at, dive_id, camera_id)
                VALUES (1, '[0.104, 0, 0]', '[0, 0, 1]', '2026-08-01', 10, 1),
                       (2, '[0.101, 0, 0]', '[0, 0.01, 1]', '2026-08-01', 12, 2);
                UPDATE dive SET calibration_refused_at = '2026-08-20',
                    calibration_refused_reason = 'baseline 8.9 cm implausible',
                    calibration_refused_labels_at = '2026-08-19'
                WHERE id = 12;
                INSERT INTO divelaserline (id, dive_id, a, b, c, n_points,
                    inlier_count, inlier_fraction, residual_std, label_noise_mad,
                    line_confidence, fitted_at)
                VALUES (1, 10, 0.6, 0.8, -1200, 40, 37, 0.925, 1.4, 0.9,
                        270256.98, '2026-08-02');
                """),
        )


def test_intrinsics_become_pinhole_calibrations_with_unknowns_kept_unknown(v1, v2):
    _seed_v1(v1)
    _seed_calibrations(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT c.v1_id, d.v1_id, c.camera_model, c.medium, c.coordinate_frame "
        "FROM camera_calibrations c JOIN devices d ON d.id = c.device_id "
        "ORDER BY c.v1_id",
    ) == [(1, 1, "pinhole", None, None), (2, 2, "pinhole", None, None)]


def test_extrinsics_become_accepted_calibrations_naming_only_what_is_certain(v1, v2):
    _seed_v1(v1)
    _seed_calibrations(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT lc.v1_id, d.v1_id, lc.outcome, lc.producer, cc.v1_id "
        "FROM laser_calibrations lc JOIN dives d ON d.id = lc.dive_id "
        "LEFT JOIN camera_calibrations cc ON cc.id = lc.camera_calibration_id "
        "WHERE lc.outcome = 'accepted' ORDER BY lc.v1_id",
    ) == [
        # d10 has a checkerboard target: slate or checkerboard -- unknown.
        (1, 10, "accepted", None, 1),
        # d12 has none: only the slate could have calibrated it.
        (2, 12, "accepted", "slate", 2),
    ]


def test_a_refusal_becomes_a_refused_row_and_v1s_current_state_holds(v1, v2):
    _seed_v1(v1)
    _seed_calibrations(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT d.v1_id, c.outcome, c.refusal_reason, c.inputs_as_of::date::text "
        "FROM current_laser_calibrations c JOIN dives d ON d.id = c.dive_id "
        "ORDER BY d.v1_id",
    ) == [
        (10, "accepted", None, None),
        (12, "refused", "baseline 8.9 cm implausible", "2026-08-19"),
    ]
    # d11 borrows d10's; d12's refusal leaves it with nothing to measure with.
    assert _rows(
        v2,
        "SELECT d.v1_id, src.v1_id FROM effective_laser_calibrations e "
        "JOIN dives d ON d.id = e.dive_id JOIN dives src ON src.id = e.source_dive_id "
        "ORDER BY d.v1_id",
    ) == [(10, 10), (11, 10)]


def test_laser_lines_arrive_intact(v1, v2):
    _seed_v1(v1)
    _seed_calibrations(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT l.v1_id, d.v1_id, l.line_confidence FROM dive_laser_lines l "
        "JOIN dives d ON d.id = l.dive_id",
    ) == [(1, 10, 270256.98)]


def test_calibrations_are_accounted_for_and_idempotent(v1, v2):
    _seed_v1(v1)
    _seed_calibrations(v1)

    report = _run(v1, v2)
    again = _run(v1, v2)

    assert report["cameraintrinsics"] == (2, 2)
    assert report["laserextrinsics"] == (2, 2)
    assert report["dive refusals"] == (1, 1)
    assert report["divelaserline"] == (1, 1)
    assert again.discrepancies() == {}
    assert _rows(v2, "SELECT count(*) FROM laser_calibrations") == [(3,)]


# --- cycle 3: labels --------------------------------------------------------------


def _seed_labels(v1: Engine) -> None:
    """A labeler; laser labels on frames 100 (human) and 101 (gate auto-accepted);
    head/tail and slate labels; a species sentinel; three sync cursors."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO "user" (id, email, first_name, label_studio_id)
            VALUES (1, 'labeler@example.test', 'Pat', 55);
            INSERT INTO laserlabel (id, label_studio_task_id, label_studio_project_id,
                x, y, label, image_id, user_id, updated_at, completed, superseded,
                needs_reprocess, label_studio_json)
            VALUES (1, 70, 7, 10.5, 20.5, 'red', 100, 1, '2026-08-01', true, false,
                    false, '{"id": 70}'),
                   (2, 71, 7, 11.0, 21.0, 'red', 101, NULL, '2026-08-01', true,
                    NULL, false, NULL);
            INSERT INTO laserprediction (id, x, y, confidence, image_id,
                auto_accept, gate_verdict, predictor_version)
            VALUES (1, 11.0, 21.0, 0.97, 101, true, 'auto_accepted', 2);
            INSERT INTO headtaillabel (id, label_studio_task_id,
                label_studio_project_id, head_x, head_y, tail_x, tail_y, image_id,
                user_id, completed, superseded, needs_reprocess)
            VALUES (1, 80, 8, 1, 2, 3, 4, 100, 1, true, true, false);
            INSERT INTO diveslatelabel (id, label_studio_task_id,
                label_studio_project_id, image_id, user_id, completed, upside_down,
                reference_points, skipped_points, needs_reprocess)
            VALUES (1, 90, 9, 102, 1, true, false, '[[1, 2]]', '[3]', true);
            INSERT INTO specieslabel (id, image_id, content_of_image, "grouping",
                top_three_photos_of_group, needs_reprocess)
            VALUES (1, 100, 'Fish, Hogfish (Lachnolaimus maximus)', NULL, true,
                    false);
            INSERT INTO labelstudiosynccursor (id, kind, label_studio_project_id,
                                               last_synced_at)
            VALUES (1, 'laser', 7, now()), (2, 'dive_slate', 9, now()),
                   (3, 'headtail', 8, now());
            """))


def test_labels_keep_their_label_studio_identity_and_state(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT l.v1_id, c.v1_id, l.ls_project_id, l.ls_task_id, l.ls_labeler_id, "
        "l.completed, l.superseded, l.x, l.label, l.ls_payload::text "
        "FROM laser_labels l JOIN captures c ON c.id = l.capture_id ORDER BY l.v1_id",
    ) == [
        (1, 100, 7, 70, 55, True, False, 10.5, "red", '{"id": 70}'),
        (2, 101, 7, 71, None, True, False, 11.0, "red", None),
    ]
    assert _rows(v2, "SELECT v1_id, superseded, tail_y FROM head_tail_labels") == [
        (1, True, 4.0)
    ]
    assert _rows(
        v2,
        "SELECT v1_id, needs_reprocess, reference_points::text, skipped_points::text "
        "FROM slate_labels",
    ) == [(1, True, "[[1, 2]]", "[3]")]


def test_a_label_names_its_source_only_when_certain(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    assert _rows(v2, "SELECT v1_id, source FROM laser_labels ORDER BY v1_id") == [
        (1, None),  # a human or an accepted pre-annotation: v1 can't say
        (2, "auto_accept"),  # the gate auto-accepted this frame's prediction
    ]
    assert _rows(v2, "SELECT source, ls_project_id FROM species_labels") == [
        ("import", None)  # a sentinel carries an imported judgement
    ]
    assert _rows(v2, "SELECT source FROM head_tail_labels") == [(None,)]


def test_no_labeler_email_or_name_crosses_into_v2(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    with v2.connect() as conn:
        dump = conn.execute(
            text(
                "SELECT string_agg(t::text, ' ') FROM "
                "(SELECT * FROM laser_labels UNION ALL SELECT * FROM laser_labels) t"
            )
        ).scalar_one()
        users = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert "labeler@example.test" not in dump and "Pat" not in dump
    assert users == 0


def test_sync_cursors_map_v1_kind_names(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    assert _rows(
        v2, "SELECT kind, ls_project_id FROM label_studio_sync_cursors ORDER BY kind"
    ) == [("head_tail", 8), ("laser", 7), ("slate", 9)]


def test_labels_are_accounted_for_and_idempotent(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    report = _run(v1, v2)
    again = _run(v1, v2)

    for table, n in [
        ("laserlabel", 2),
        ("headtaillabel", 1),
        ("diveslatelabel", 1),
        ("specieslabel", 1),
        ("labelstudiosynccursor", 3),
    ]:
        assert report[table] == (n, n)
    assert again.discrepancies() == {}


# --- cycle 4: predictions ---------------------------------------------------------


def _seed_predictions(v1: Engine) -> None:
    """On top of _seed_labels' laser prediction: a slate prediction with both
    points and a reason (49 such rows in production) and a head/tail
    prediction cropped around laser label 1."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO slateprediction (id, reference_points, confidence,
                rejected_reason, width, height, created_at, image_id)
            VALUES (1, '[[1.0, 2.0]]', 0.4, 'low_confidence', 4000, 3000,
                    '2026-08-02', 102);
            INSERT INTO headtailprediction (id, head_x, head_y, tail_x, tail_y,
                laser_label_id, predictor_version, checkpoint, core_version,
                status, rejected_low_confidence, created_at, image_id)
            VALUES (1, 1, 2, 3, 4, 1, 2, 'sam3.1_multiplex.pt', '4.0.0',
                    'predicted', false, '2026-09-03', 100);
            """))


def test_predictions_keep_their_provenance_and_links(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)
    _seed_predictions(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT p.v1_id, c.v1_id, p.auto_accept, p.gate_verdict, "
        "p.predictor_version FROM laser_predictions p "
        "JOIN captures c ON c.id = p.capture_id",
    ) == [(1, 101, True, "auto_accepted", 2)]
    assert _rows(
        v2,
        "SELECT p.v1_id, p.status, p.checkpoint, l.v1_id FROM head_tail_predictions p "
        "JOIN laser_labels l ON l.id = p.laser_label_id",
    ) == [(1, "predicted", "sam3.1_multiplex.pt", 1)]
    assert _rows(
        v2,
        "SELECT v1_id, rejected_reason, reference_points::text FROM slate_predictions",
    ) == [(1, "low_confidence", "[[1.0, 2.0]]")]


def test_predictions_are_accounted_for_and_current(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)
    _seed_predictions(v1)

    report = _run(v1, v2)

    for table in ("laserprediction", "slateprediction", "headtailprediction"):
        assert report[table] == (1, 1)
    assert _rows(
        v2,
        "SELECT (SELECT count(*) FROM current_laser_predictions), "
        "(SELECT count(*) FROM current_slate_predictions), "
        "(SELECT count(*) FROM current_head_tail_predictions)",
    ) == [(1, 1, 1)]


# --- cycle 5: fish and clusters -----------------------------------------------------


def _seed_fish(v1: Engine) -> None:
    """A real hogfish, the Ruler model, and two clusters on d10."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO fish (id, name, species_id) VALUES (1, NULL, 1),
                                                           (2, 'Ruler', NULL);
            INSERT INTO diveframecluster (id, data_source, updated_at, dive_id,
                                          fish_id)
            VALUES (1, 'PREDICTION', now(), 10, NULL),
                   (2, 'LABEL_STUDIO', now(), 10, 1);
            INSERT INTO diveframeclusterimagemapping (dive_frame_cluster_id,
                                                      image_id)
            VALUES (1, 100), (2, 100);
            """))


def test_fish_reach_their_species_or_model_by_key(v1, v2):
    _seed_v1(v1)
    _seed_fish(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT f.v1_id, s.scientific_name, m.name FROM fish f "
        "LEFT JOIN species s ON s.id = f.species_id "
        "LEFT JOIN fish_models m ON m.id = f.fish_model_id ORDER BY f.v1_id",
    ) == [(1, "Lachnolaimus maximus", None), (2, None, "Ruler")]


def test_clusters_keep_their_formation_fish_and_members(v1, v2):
    _seed_v1(v1)
    _seed_fish(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT k.v1_id, k.formed_by, d.v1_id, f.v1_id FROM dive_frame_clusters k "
        "JOIN dives d ON d.id = k.dive_id LEFT JOIN fish f ON f.id = k.fish_id "
        "ORDER BY k.v1_id",
    ) == [(1, "prediction", 10, None), (2, "label_studio", 10, 1)]
    assert _rows(
        v2,
        "SELECT k.v1_id, c.v1_id FROM dive_frame_cluster_captures m "
        "JOIN dive_frame_clusters k ON k.id = m.cluster_id "
        "JOIN captures c ON c.id = m.capture_id ORDER BY k.v1_id",
    ) == [(1, 100), (2, 100)]


def test_fish_and_clusters_are_accounted_for_and_idempotent(v1, v2):
    _seed_v1(v1)
    _seed_fish(v1)

    report = _run(v1, v2)
    again = _run(v1, v2)

    assert report["fish"] == (2, 2)
    assert report["diveframecluster"] == (2, 2)
    assert report["diveframeclusterimagemapping"] == (2, 2)
    assert again.discrepancies() == {}


# --- cycle 6: depths and measurements ------------------------------------------------


def _seed_results(v1: Engine) -> None:
    """A depth on frame 100; measurements on frame 100 (dive 10, calibration
    still effective) and frame 101 (dive 12, whose calibration was refused
    after it was measured)."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO laserdepth (id, depth_m, range_m, residual_m, created_at,
                image_id, laser_label_id, laser_extrinsics_id)
            VALUES (1, 1.5, 1.52, 0.001, '2026-08-18', 100, 1, 1);
            INSERT INTO measurement (id, length_m, image_id, fish_id,
                                     laser_extrinsics_id)
            VALUES (1, 0.412, 100, 1, 1), (2, 0.377, 101, 1, 2);
            """))


def _seed_everything(v1: Engine) -> None:
    for seed in (_seed_v1, _seed_calibrations, _seed_labels, _seed_fish, _seed_results):
        seed(v1)


def test_depths_keep_their_label_and_calibration(v1, v2):
    _seed_everything(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT d.v1_id, c.v1_id, l.v1_id, k.v1_id, d.depth_m, d.core_version "
        "FROM laser_depths d JOIN captures c ON c.id = d.capture_id "
        "JOIN laser_labels l ON l.id = d.laser_label_id "
        "JOIN laser_calibrations k ON k.id = d.laser_calibration_id",
    ) == [(1, 100, 1, 1, 1.5, None)]


def test_measurements_arrive_as_server_results_with_unknown_provenance(v1, v2):
    _seed_everything(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT m.v1_id, c.v1_id, f.v1_id, m.source, m.length_m, k.v1_id, "
        "m.algorithm, m.core_version FROM measurements m "
        "JOIN captures c ON c.id = m.capture_id JOIN fish f ON f.id = m.fish_id "
        "JOIN laser_calibrations k ON k.id = m.laser_calibration_id "
        "ORDER BY m.v1_id",
    ) == [
        (1, 100, 1, "server", 0.412, 1, None, None),
        (2, 101, 1, "server", 0.377, 2, None, None),
    ]


def test_a_measurement_on_a_later_refused_dive_is_not_current(v1, v2):
    """v1 keeps showing it; v2's §9.13 rule does not -- a documented change."""
    _seed_everything(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT m.v1_id FROM current_measurements m ORDER BY m.v1_id",
    ) == [(1,)]


def test_results_are_accounted_for_and_idempotent(v1, v2):
    _seed_everything(v1)

    report = _run(v1, v2)
    again = _run(v1, v2)

    assert report["laserdepth"] == (1, 1)
    assert report["measurement"] == (2, 2)
    assert again.discrepancies() == {}


def _zero_length_measurement(v1: Engine) -> None:
    """v1 stored whatever its activity computed, including a 0 m length (a
    degenerate head/tail, or a dot on the optical axis). v2's
    measurements_length_check refuses it; v2's own stage 14 records such a
    length as a refusal, never a measurement."""
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO measurement (id, length_m, image_id, fish_id,
                                     laser_extrinsics_id)
            VALUES (3, 0.0, 100, 2, 1);
            """))


def test_a_zero_length_measurement_is_skipped_and_reported(v1, v2):
    """One such row must not abort the one-transaction migration. It is not
    migrated, and the report says so: every v1 row is either migrated or
    skipped by name, so it is no discrepancy."""
    _seed_everything(v1)
    _zero_length_measurement(v1)

    report = _run(v1, v2)

    assert report["measurement"] == (3, 2)
    assert report.skipped == {"measurement": {"non-positive length": 1}}
    assert report.discrepancies() == {}
    assert _rows(v2, "SELECT v1_id FROM measurements ORDER BY v1_id") == [(1,), (2,)]


# --- cycle 7: the migrate-v1 command and its go/no-go validation --------------------


def _cli_env(monkeypatch, v1: Engine, v2: Engine) -> None:
    monkeypatch.setenv("FISHSENSE_V1_DATABASE_URL", v1.url.render_as_string(False))
    monkeypatch.setenv(
        "FISHSENSE_MIGRATION_DATABASE_URL", v2.url.render_as_string(False)
    )


async def test_migrate_v1_says_go_when_everything_checks_out(
    v1, v2, monkeypatch, capsys
):
    _seed_everything(v1)
    _cli_env(monkeypatch, v1, v2)

    assert await main(["migrate-v1"]) == 0
    out = capsys.readouterr().out
    assert "GO" in out and "NO-GO" not in out
    assert "measurement parity: 1 current in v2 = 1 fresh in v1" in out
    # v1 still shows it; v2 intentionally doesn't (PLAN 9.13) -- reported, not a gap.
    assert "1 on refused dives" in out


async def test_migrate_v1_goes_with_a_skipped_zero_length_measurement(
    v1, v2, monkeypatch, capsys
):
    """The skipped row is printed, and parity reads v1 without it: v1 showed
    a 0 m fish, v2 has no such measurement to count."""
    _seed_everything(v1)
    _zero_length_measurement(v1)
    _cli_env(monkeypatch, v1, v2)

    assert await main(["migrate-v1"]) == 0
    out = capsys.readouterr().out
    assert "GO" in out and "NO-GO" not in out
    assert "skipped measurement: 1 non-positive length" in out
    assert "measurement parity: 1 current in v2 = 1 fresh in v1" in out


async def test_migrate_v1_says_no_go_on_a_schema_not_at_head(
    v1, server, owner_url, monkeypatch, capsys
):
    behind = _create(server, "behind")
    await upgrade(_url(owner_url, behind), app_role=APP_ROLE, revision="0010")
    monkeypatch.setenv("FISHSENSE_V1_DATABASE_URL", v1.url.render_as_string(False))
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", _url(owner_url, behind))

    assert await main(["migrate-v1"]) != 0
    assert "run `fishsense-services-api migrate` first" in capsys.readouterr().err


async def test_migrate_v1_refuses_a_role_that_rls_would_block(
    v1, v2, monkeypatch, capsys
):
    """FORCE RLS binds the table owner too: only a role that bypasses RLS can
    write every tenant's rows. The app role can't, so it's refused up front."""
    as_app = v2.url.set(username=APP_ROLE, password=APP_ROLE)
    monkeypatch.setenv("FISHSENSE_V1_DATABASE_URL", v1.url.render_as_string(False))
    monkeypatch.setenv(
        "FISHSENSE_MIGRATION_DATABASE_URL", as_app.render_as_string(False)
    )

    assert await main(["migrate-v1"]) != 0
    assert "BYPASSRLS" in capsys.readouterr().err


async def test_migrate_v1_names_missing_configuration(monkeypatch, capsys):
    monkeypatch.delenv("FISHSENSE_V1_DATABASE_URL", raising=False)
    monkeypatch.delenv("FISHSENSE_MIGRATION_DATABASE_URL", raising=False)

    assert await main(["migrate-v1"]) != 0
    err = capsys.readouterr().err
    assert "FISHSENSE_V1_DATABASE_URL" in err
    assert "FISHSENSE_MIGRATION_DATABASE_URL" in err


# -- laser provenance from fishsense-lite #927/#932 --------------------------------


def _v1_with_superseded_reason(v1: Engine) -> None:
    """v1 at alembic e5a9c3d71b24 (fishsense-lite #932): laserlabel records why
    a label was superseded. The rehearsal dump predates it."""
    with v1.begin() as conn:
        conn.execute(
            text("ALTER TABLE laserlabel ADD COLUMN superseded_reason varchar(40)")
        )


def test_a_superseded_reason_is_carried_when_v1_records_one(v1, v2):
    """Including `remediation` on a live row: it means "last changed by the
    reviewed remediation", not "superseded"."""
    _seed_v1(v1)
    _v1_with_superseded_reason(v1)
    _seed_labels(v1)
    with v1.begin() as conn:
        conn.execute(text("""
            UPDATE laserlabel SET superseded = true,
                superseded_reason = 'validator_3sigma' WHERE id = 1;
            UPDATE laserlabel SET superseded = false,
                superseded_reason = 'remediation' WHERE id = 2;
            """))

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT v1_id, superseded, superseded_reason FROM laser_labels ORDER BY v1_id",
    ) == [(1, True, "validator_3sigma"), (2, False, "remediation")]


def test_a_v1_without_superseded_reasons_leaves_them_unknown(v1, v2):
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    assert _rows(v2, "SELECT DISTINCT superseded_reason FROM laser_labels") == [(None,)]


def test_laser_lines_say_which_noise_estimator_made_them(v1, v2):
    """fishsense-core 4.1.0 (deployed to v1 at 2026-09-26T22:19:45Z) estimates
    label noise from *signed* residuals; before it, v1 took the MAD of absolute
    distances, about 0.59 sigma. v1 keeps one line per dive, rewritten on each
    run with `fitted_at` stamped, and no validator ran between NRP deleting
    the workers (09-21) and that deploy -- so `fitted_at` says which."""
    _seed_v1(v1)
    _seed_calibrations(v1)
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO divelaserline (id, dive_id, a, b, c, n_points,
                inlier_count, inlier_fraction, residual_std, label_noise_mad,
                line_confidence, fitted_at)
            VALUES (2, 12, 0.6, 0.8, -1200, 40, 38, 0.95, 1.4, 1.85, 30.0,
                    '2026-09-26T23:00:00Z');
            """))

    _run(v1, v2)

    assert _rows(
        v2, "SELECT v1_id, noise_estimator FROM dive_laser_lines ORDER BY v1_id"
    ) == [(1, "absolute_residual_mad"), (2, "signed_residual_mad")]


# -- numbers (migration 0019) ------------------------------------------------------


def test_migrated_rows_keep_v1s_ids_as_their_numbers(v1, v2):
    """Label Studio titles embed v1's dive id, so every existing project is
    still found by its dive's number."""
    _seed_v1(v1)

    _run(v1, v2)

    assert all(
        number == v1_id
        for number, v1_id in _rows(v2, "SELECT number, v1_id FROM dives")
        + _rows(v2, "SELECT number, v1_id FROM captures")
    )


def test_rows_the_migration_creates_are_numbered_above_v1s(v1, v2):
    """A refusal becomes its own laser_calibrations row with no v1 id. Numbered
    from 1 it would take the number a later v1 fit needs, and that fit's
    insert would be silently dropped as a conflict."""
    _seed_v1(v1)
    _seed_calibrations(v1)

    _run(v1, v2)

    refusals = _rows(
        v2, "SELECT number FROM laser_calibrations WHERE outcome = 'refused'"
    )
    largest_v1 = _rows(v1, "SELECT max(id) FROM laserextrinsics")[0][0]
    assert refusals and all(number > largest_v1 for (number,) in refusals)
    assert (
        _rows(v2, "SELECT count(*) FROM laser_calibrations WHERE v1_id IS NOT NULL")[0][
            0
        ]
        == _rows(v1, "SELECT count(*) FROM laserextrinsics")[0][0]
    )


def test_a_new_row_after_the_migration_is_numbered_above_v1s(v1, v2):
    _seed_v1(v1)

    _run(v1, v2)

    with v2.begin() as conn:
        tenant = conn.execute(text("SELECT id FROM tenants")).scalar_one()
        new = conn.execute(
            text(
                "INSERT INTO dives (tenant_id, source_path, dived_at) "
                "VALUES (:t, 'new', now()) RETURNING number"
            ),
            {"t": tenant},
        ).scalar_one()
    assert new > _rows(v1, "SELECT max(id) FROM dive")[0][0]


def test_label_studio_projects_are_recorded_from_v1s_labels(v1, v2):
    """v1 found projects by title; v2 records them, from the projects v1's
    labels point at. A sentinel (no project) records nothing. A project whose
    labels span dives (project 7: dives 10 and 12) is recorded against the dive
    holding most of them, ties to the lowest number."""
    _seed_v1(v1)
    _seed_labels(v1)

    _run(v1, v2)

    assert _rows(
        v2,
        "SELECT p.kind, p.ls_project_id, d.v1_id FROM label_studio_projects p "
        "JOIN dives d ON d.id = p.dive_id ORDER BY p.ls_project_id",
    ) == [("laser", 7, 10), ("head_tail", 8, 10), ("slate", 9, 11)]


# -- depth-measure: the parity gate reads v1 the way v2 reads its copy ---------------
#
# `measurement_parity` compares v2's `current_measurements` with v1's own rule
# for a fresh measurement. Both sides must mean the same thing, or the
# rehearsal reports a gap that is really a difference of definition:
#
# * the calibration is v1's read rule (fishsense-lite@77e8f8e5
#   dive_controller.get_laser_extrinsics_for_dive, and v2's 0018): the dive's
#   own plausible fit, else its link's. `_V1_FRESH` followed the link first
#   and applied no plausibility test;
# * a measurement bound to a fish the frame's subject no longer names (#527,
#   #905) is one v1 deletes on its next measure run and v2 no longer counts
#   (0026). It is reported beside the refused ones, not as a gap.


def _parity(v1: Engine, v2: Engine):
    return measurement_parity(
        v1.url.render_as_string(hide_password=False),
        v2.url.render_as_string(hide_password=False),
    )


def test_a_binding_the_label_no_longer_names_is_stale_on_both_sides(v1, v2):
    """Frame 103 is labelled Grouper but measured as the Snook (a relabel
    after the measurement): v1 would delete it on its next run."""
    _seed_everything(v1)
    with v1.begin() as conn:
        conn.execute(text(f"""
                INSERT INTO fish (id, name, species_id) VALUES (3, 'Snook', NULL);
                INSERT INTO image (id, path, taken_datetime, checksum, is_canonical,
                                   dive_id, camera_id)
                VALUES (103, 'dives/d10/P2.ORF', now(), '{"e" * 32}', true, 10, 1);
                INSERT INTO specieslabel (id, image_id, content_of_image,
                    top_three_photos_of_group, label_studio_project_id, superseded,
                    needs_reprocess)
                VALUES (2, 103, 'Fish Model, Grouper', true, 70, false, false);
                INSERT INTO measurement (id, length_m, image_id, fish_id,
                                         laser_extrinsics_id)
                VALUES (3, 0.44, 103, 3, 1);
                """))

    _run(v1, v2)

    assert _parity(v1, v2) == (1, 1, 1, 1)
    assert _rows(v2, "SELECT v1_id FROM current_measurements") == [(1,)]


def test_a_stale_binding_on_a_low_priority_dive_is_fresh_on_both_sides(v1, v2):
    """v1's stage 14, the only thing that deleted a stale binding, never runs
    on a low-priority dive: v1 keeps showing the Snook, and v2 counts it."""
    _seed_everything(v1)
    with v1.begin() as conn:
        conn.execute(text(f"""
                UPDATE dive SET priority = 'LOW' WHERE id = 10;
                INSERT INTO fish (id, name, species_id) VALUES (3, 'Snook', NULL);
                INSERT INTO image (id, path, taken_datetime, checksum, is_canonical,
                                   dive_id, camera_id)
                VALUES (103, 'dives/d10/P2.ORF', now(), '{"e" * 32}', true, 10, 1);
                INSERT INTO specieslabel (id, image_id, content_of_image,
                    top_three_photos_of_group, label_studio_project_id, superseded,
                    needs_reprocess)
                VALUES (2, 103, 'Fish Model, Grouper', true, 70, false, false);
                INSERT INTO measurement (id, length_m, image_id, fish_id,
                                         laser_extrinsics_id)
                VALUES (3, 0.44, 103, 3, 1);
                """))

    _run(v1, v2)

    assert _parity(v1, v2) == (2, 2, 1, 0)
    assert _rows(v2, "SELECT v1_id FROM current_measurements ORDER BY v1_id") == [
        (1,),
        (3,),
    ]


def test_a_real_fish_bound_off_its_cluster_is_stale_on_both_sides(v1, v2):
    """Frame 100's Label Studio cluster now points at fish 4, while its
    measurement is bound to fish 1 (dives 341/383's shape)."""
    _seed_everything(v1)
    with v1.begin() as conn:
        conn.execute(text("""
            UPDATE specieslabel SET label_studio_project_id = 70 WHERE id = 1;
            INSERT INTO fish (id, name, species_id) VALUES (4, NULL, 1);
            UPDATE diveframecluster SET fish_id = 4 WHERE id = 2;
            """))

    _run(v1, v2)

    assert _parity(v1, v2) == (0, 0, 1, 1)


def test_a_dives_own_calibration_wins_over_its_link_on_both_sides(v1, v2):
    """d11 borrows d10's calibration and has a fit of its own: its own wins,
    in v1's read rule and in v2's effective calibration."""
    _seed_everything(v1)
    with v1.begin() as conn:
        conn.execute(text("""
            INSERT INTO laserextrinsics (id, laser_position, laser_axis,
                                         created_at, dive_id, camera_id)
            VALUES (3, '[0.1, 0, 0]', '[0, 0, 1]', '2026-08-05', 11, 1);
            INSERT INTO measurement (id, length_m, image_id, fish_id,
                                     laser_extrinsics_id)
            VALUES (3, 0.3, 102, 1, 3);
            """))

    _run(v1, v2)

    assert _parity(v1, v2) == (2, 2, 1, 0)


def test_an_implausible_calibration_counts_as_none_on_both_sides(v1, v2):
    """v1's read endpoint treated a 2.35 cm baseline as no calibration (its
    `_plausible_extrinsics`), and so does v2's effective calibration."""
    _seed_everything(v1)
    with v1.begin() as conn:
        conn.execute(text("""
            UPDATE laserextrinsics SET laser_position = '[0.0141, 0.0188, 0]'
            WHERE id = 1;
            """))

    _run(v1, v2)

    assert _parity(v1, v2) == (0, 0, 1, 0)
