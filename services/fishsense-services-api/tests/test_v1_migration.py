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

from fishsense_services_api.migrations import upgrade
from fishsense_services_api.v1_migration import migrate_v1

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
