"""The schema the laser-depth and stage-14 stages add (0025..03).

* the refusal tables are append-only and tenant-scoped, like the results they
  stand in for;
* species and fish models -- global tables the app role may not write -- gain
  one way in: find-or-create an identity, never an edit;
* the SQL that classifies a species label (`measurement_subjects`,
  `fish_model_name`) agrees with fishsense-lite@77e8f8e5's taxonomy, the
  definition of record, over the shared `MEASURABILITY_CORPUS` -- the same
  guard v1's test_dive_pipeline_status_view.py ran for its view;
* `current_measurements` keeps 0013's rules for everything the binding rule
  does not touch (a device's own measurement).
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    forget_identities,
    capture,
    exec_,
    fish,
    laser_label,
    species_label,
    tenant,
)
from fishsense_services_api import taxonomy_sql
from fishsense_services_api.db import tenant_transaction
from fishsense_services_contracts import taxonomy

CORPUS = [content for content, _ in taxonomy.MEASURABILITY_CORPUS]


# -- the refusal tables ---------------------------------------------------------


async def _depth_refusal(conn, tenant_id, capture_id, label, calibration):
    return (
        await conn.execute(
            text(
                "INSERT INTO laser_depth_refusals (tenant_id, capture_id, "
                "laser_label_id, laser_x, laser_y, laser_calibration_id, reason, "
                "depth_m, core_version) VALUES (:t, :c, :l, 1, 2, :cal, "
                "'non_positive_depth', -0.5, '4.1.0') RETURNING id"
            ),
            {"t": tenant_id, "c": capture_id, "l": label, "cal": calibration},
        )
    ).scalar_one()


async def test_refusals_are_appended_never_rewritten(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    label = await laser_label(owner_engine, lab, capture_id)

    async with tenant_transaction(app_engine, lab) as conn:
        await _depth_refusal(conn, lab, capture_id, label, calibration)

    for statement in (
        "UPDATE laser_depth_refusals SET reason = 'non_finite_depth'",
        "DELETE FROM laser_depth_refusals",
        "UPDATE measurement_refusals SET reason = 'zero_length'",
        "DELETE FROM measurement_refusals",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, lab) as conn:
                await conn.execute(text(statement))


async def test_a_tenant_sees_only_its_own_refusals(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine, "lab"), await tenant(owner_engine, "reef")
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    label = await laser_label(owner_engine, lab, capture_id)
    async with tenant_transaction(app_engine, lab) as conn:
        await _depth_refusal(conn, lab, capture_id, label, calibration)

    async with tenant_transaction(app_engine, reef) as conn:
        seen = (
            await conn.execute(text("SELECT count(*) FROM laser_depth_refusals"))
        ).scalar_one()
        with pytest.raises(DBAPIError, match="row-level security"):
            await _depth_refusal(conn, lab, capture_id, label, calibration)

    assert seen == 0


async def test_a_refusal_never_holds_a_usable_depth(owner_engine):
    """A positive depth would have been a depth."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    label = await laser_label(owner_engine, lab, capture_id)

    with pytest.raises(DBAPIError, match="laser_depth_refusals_depth_m_check"):
        await exec_(
            owner_engine,
            "INSERT INTO laser_depth_refusals (tenant_id, capture_id, "
            "laser_label_id, laser_x, laser_y, laser_calibration_id, reason, "
            "depth_m, core_version) VALUES (:t, :c, :l, 1, 2, :cal, "
            "'non_positive_depth', 1.2, '4.1.0')",
            t=lab,
            c=capture_id,
            l=label,
            cal=calibration,
        )


# -- species and fish models: find or create, nothing more ------------------------


async def test_the_app_role_still_cannot_write_species_or_fish_models(
    owner_engine, app_engine
):
    """The audit's rule for global tables stands: no table grant."""
    lab = await tenant(owner_engine)
    for statement in (
        "INSERT INTO species (scientific_name) VALUES ('Nope nope')",
        "INSERT INTO fish_models (name) VALUES ('Nope')",
        "UPDATE species SET common_name = 'x'",
        "DELETE FROM fish_models",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_transaction(app_engine, lab) as conn:
                await conn.execute(text(statement))


async def test_ensure_species_finds_or_creates_and_never_edits(
    owner_engine, app_engine
):
    """v1's `_ensure_species`: find by scientific name, else create."""
    lab = await tenant(owner_engine)
    scientific = f"Caranx ruber {uuid.uuid4().hex[:6]}"

    async with tenant_transaction(app_engine, lab) as conn:
        first = (
            await conn.execute(
                text("SELECT ensure_species(:s, 'Bar Jack')"), {"s": scientific}
            )
        ).scalar_one()
        again = (
            await conn.execute(
                text("SELECT ensure_species(:s, 'Renamed')"), {"s": scientific}
            )
        ).scalar_one()
        row = (
            await conn.execute(
                text("SELECT common_name FROM species WHERE id = :i"), {"i": first}
            )
        ).scalar_one()

    assert first == again
    assert row == "Bar Jack", "an existing species is returned as it is"


async def test_ensure_fish_model_finds_or_creates(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    name = f"Model {uuid.uuid4().hex[:6]}"

    async with tenant_transaction(app_engine, lab) as conn:
        first = (
            await conn.execute(text("SELECT ensure_fish_model(:n)"), {"n": name})
        ).scalar_one()
        again = (
            await conn.execute(text("SELECT ensure_fish_model(:n)"), {"n": name})
        ).scalar_one()
        lengths = (
            await conn.execute(
                text("SELECT count(*) FROM fish_model_references WHERE name = :n"),
                {"n": name},
            )
        ).scalar_one()

    assert first == again
    assert lengths == 0, "an identity, never a reference length"


@pytest.mark.parametrize(
    "call",
    [
        "SELECT ensure_species('', 'x')",
        "SELECT ensure_species('  ', 'x')",
        "SELECT ensure_species(NULL, 'x')",
        "SELECT ensure_fish_model('')",
        "SELECT ensure_fish_model(NULL)",
    ],
)
async def test_an_identity_needs_a_name(owner_engine, app_engine, call):
    lab = await tenant(owner_engine)

    with pytest.raises(DBAPIError, match="needs a"):
        async with tenant_transaction(app_engine, lab) as conn:
            await conn.execute(text(call))


async def test_only_the_app_role_may_call_them(owner_engine):
    """EXECUTE is revoked from PUBLIC: a role the migration did not name
    cannot add to shared reference data."""
    async with owner_engine.begin() as conn:
        grantees = set((await conn.execute(text("""
                        SELECT DISTINCT grantee FROM information_schema.routine_privileges
                        WHERE routine_name IN ('ensure_species', 'ensure_fish_model')
                          AND privilege_type = 'EXECUTE'
                        """))).scalars())
        definer = (await conn.execute(text("""
                    SELECT bool_and(prosecdef AND proconfig IS NOT NULL)
                    FROM pg_proc
                    WHERE proname IN ('ensure_species', 'ensure_fish_model')
                    """))).scalar_one()

    assert "PUBLIC" not in grantees
    assert "fishsense_app" in grantees
    assert definer, "SECURITY DEFINER with a pinned search_path"


# -- the classification agrees with the taxonomy --------------------------------------


async def _sql_over_corpus(owner_engine, expression: str) -> dict:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                f"SELECT c, {expression.format(c='c')} AS v "
                "FROM unnest(CAST(:corpus AS text[])) AS t(c)"
            ),
            {"corpus": CORPUS},
        )
        return {r.c: r.v for r in rows}


async def test_fish_model_name_is_parse_model_name(owner_engine):
    """The stage-14 binding reads the model's name in SQL; the definition of
    record is `parse_model_name`. They must agree exactly, or a measurement
    bound in Python reads as stale in the view (or the reverse)."""
    names = await _sql_over_corpus(owner_engine, "fish_model_name({c})")

    assert names == {c: taxonomy.parse_model_name(c) for c in CORPUS}


async def test_fish_model_name_names_exactly_the_rigid_targets(owner_engine):
    """...and it names something exactly where the cohort predicates'
    `rigid_target_sql` says the row is a rigid target."""
    named = await _sql_over_corpus(owner_engine, "fish_model_name({c}) IS NOT NULL")
    rigid = await _sql_over_corpus(
        owner_engine, "coalesce(" + taxonomy_sql.rigid_target_sql("{c}") + ", false)"
    )

    assert named == rigid


async def test_the_subjects_measurable_rows_are_the_taxonomys(owner_engine):
    """`measurement_subjects` classifies a row as a real fish or a named
    target; together those are `measurable_species_sql` -- including its
    pinned divergence (`SQL_BROADER_THAN_PYTHON`), which the store refuses
    rather than wedging on."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    probes = CORPUS + list(taxonomy.SQL_BROADER_THAN_PYTHON)
    by_capture = {}
    for content in probes:
        capture_id = await capture(owner_engine, lab, dive_id)
        await species_label(owner_engine, lab, capture_id, content)
        by_capture[capture_id] = content

    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT capture_id, real_fish OR model_name IS NOT NULL AS measurable, "
                "coalesce("
                + taxonomy_sql.measurable_species_sql("content_of_image")
                + ", false) AS expected FROM measurement_subjects WHERE tenant_id = :t"
            ),
            {"t": lab},
        )
        verdicts = {r.capture_id: (r.measurable, r.expected) for r in rows}

    assert set(verdicts) == set(by_capture)
    assert all(ours == expected for ours, expected in verdicts.values()), {
        by_capture[c]: v for c, v in verdicts.items() if v[0] != v[1]
    }


# -- the kernel's preconditions ------------------------------------------------------


@pytest.mark.parametrize(
    "value, usable",
    [
        ([0, 0, 1], True),
        ([0.0, -0.02, 1.0], True),
        ([0, 0, 0], False),
        ([0, 1], False),
        (["0", 0, 1], False),
        ([0, None, 1], False),
        ({"x": 1}, False),
        (None, False),
    ],
)
async def test_usable_laser_axis(owner_engine, value, usable):
    async with owner_engine.connect() as conn:
        got = (
            await conn.execute(
                text("SELECT usable_laser_axis(CAST(:v AS jsonb))"),
                {"v": None if value is None else json.dumps(value)},
            )
        ).scalar_one()
    assert got is usable


@pytest.mark.parametrize(
    "matrix, usable",
    [
        ([[3000, 0, 2048], [0, 3000, 1536], [0, 0, 1]], True),
        ([[1, 2, 3], [4, 5, 6], [7, 8, 9]], False),
        ([[0, 0, 0], [0, 0, 0], [0, 0, 0]], False),
        ([[1, 0, 0], [0, "1", 0], [0, 0, 1]], False),
        ([[1, 0], [0, 1]], False),
    ],
)
async def test_usable_camera_matrix(owner_engine, matrix, usable):
    async with owner_engine.connect() as conn:
        got = (
            await conn.execute(
                text("SELECT usable_camera_matrix(CAST(:m AS jsonb))"),
                {"m": json.dumps(matrix)},
            )
        ).scalar_one()
    assert got is usable


# -- current_measurements keeps 0013's rules --------------------------------------------


async def test_a_devices_own_measurement_is_never_a_stale_binding(owner_engine):
    """The binding rule is v1's, for server results. A device measures what it
    measures, against no server calibration (0013)."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id)
    await species_label(owner_engine, lab, capture_id, "Fish Model, Grouper")
    snook = await fish(owner_engine, lab, model="Snook")
    await exec_(
        owner_engine,
        "INSERT INTO measurements (tenant_id, capture_id, fish_id, source, "
        "length_m) VALUES (:t, :c, :f, 'device', 0.4)",
        t=lab,
        c=capture_id,
        f=snook,
    )

    async with owner_engine.connect() as conn:
        current = (
            (
                await conn.execute(
                    text(
                        "SELECT source FROM current_measurements WHERE capture_id = :c"
                    ),
                    {"c": capture_id},
                )
            )
            .scalars()
            .all()
        )

    assert current == ["device"]
