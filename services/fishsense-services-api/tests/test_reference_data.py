"""Global reference data: shared by every tenant, read-only to the API.

Species, calibration targets, the known lengths of fish models, and dive-slate
templates are facts about the world, not about a tenant (PLAN.md §4.3). The
app role reads them in any transaction and never writes them.

Reference values are **versioned**, never edited in place: v1's live values
silently drifted from its seeds (Weasly Fish 0.310 → 0.313, §2.7). A correction
is a new version with its own ``valid_from``; the ``current_*`` views give the
latest per name, and every earlier value stays queryable.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from fishsense_services_api.db import principal_transaction

REFERENCE_TABLES = [
    "species",
    "calibration_targets",
    "fish_models",
    "fish_model_references",
    "slate_templates",
]


async def _insert(owner_engine, sql: str, **params) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text(sql), params)


@pytest.fixture(autouse=True)
async def _clean_reference_data(owner_engine):
    yield
    async with owner_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(REFERENCE_TABLES)} CASCADE"))


async def test_the_app_role_reads_reference_data_without_a_tenant(
    owner_engine, app_engine
):
    await _insert(
        owner_engine,
        "INSERT INTO species (scientific_name, common_name) "
        "VALUES ('Lachnolaimus maximus', 'Hogfish')",
    )

    async with principal_transaction(app_engine, "sub-anyone") as conn:
        names = (await conn.execute(text("SELECT common_name FROM species"))).scalars()
        assert list(names) == ["Hogfish"]


@pytest.mark.parametrize("table", REFERENCE_TABLES)
async def test_the_app_role_cannot_write_reference_data(app_engine, table):
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {table}"))


async def test_a_corrected_reference_length_is_a_new_version(owner_engine, app_engine):
    await _insert(owner_engine, "INSERT INTO fish_models (name) VALUES ('Weasly Fish')")
    for length, valid_from in [
        (0.310, datetime(2026, 8, 4, tzinfo=UTC)),
        (0.313, datetime(2026, 9, 12, tzinfo=UTC)),
    ]:
        await _insert(
            owner_engine,
            "INSERT INTO fish_model_references (name, known_length_m, valid_from) "
            "VALUES ('Weasly Fish', :length, :valid_from)",
            length=length,
            valid_from=valid_from,
        )

    async with app_engine.begin() as conn:
        current = (
            await conn.execute(
                text(
                    "SELECT known_length_m FROM current_fish_model_references "
                    "WHERE name = 'Weasly Fish'"
                )
            )
        ).scalar_one()
        history = (
            await conn.execute(
                text(
                    "SELECT known_length_m FROM fish_model_references "
                    "WHERE name = 'Weasly Fish' ORDER BY valid_from"
                )
            )
        ).scalars()

    assert current == pytest.approx(0.313)
    assert list(history) == pytest.approx([0.310, 0.313])


async def test_calibration_target_pitch_is_per_axis(owner_engine, app_engine):
    """The E4E board is ~0.7 % anisotropic (wuwnet, §2.7): one scalar hides it."""
    await _insert(
        owner_engine,
        "INSERT INTO calibration_targets "
        "(name, interior_rows, interior_cols, pitch_x_m, pitch_y_m, valid_from) "
        "VALUES ('E4E Checkerboard', 10, 14, 0.04223, 0.04211, '2026-09-15')",
    )

    async with app_engine.begin() as conn:
        pitch = (
            await conn.execute(
                text(
                    "SELECT pitch_x_m, pitch_y_m FROM current_calibration_targets "
                    "WHERE name = 'E4E Checkerboard'"
                )
            )
        ).one()

    assert tuple(pitch) == pytest.approx((0.04223, 0.04211))


async def test_a_species_scientific_name_is_unique(owner_engine):
    await _insert(
        owner_engine, "INSERT INTO species (scientific_name) VALUES ('Mycteroperca')"
    )

    with pytest.raises(DBAPIError, match="duplicate key"):
        await _insert(
            owner_engine,
            "INSERT INTO species (scientific_name) VALUES ('Mycteroperca')",
        )


async def test_a_length_can_be_recorded_only_for_a_registered_fish_model(
    owner_engine,
):
    """The model's identity is a row, not a string that might be misspelled."""
    with pytest.raises(DBAPIError, match="foreign key"):
        await _insert(
            owner_engine,
            "INSERT INTO fish_model_references (name, known_length_m) "
            "VALUES ('Wesly Fish', 0.313)",
        )
