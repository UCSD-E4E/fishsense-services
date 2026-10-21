"""What the signed-out page may say (docs/design/README.md, Home pages; the
owner, 2026-10-20): live totals, and wild fish by broad region -- rounded, and
"under 25" below 25 -- from tenants that opted in, and nothing else.
"""

from __future__ import annotations

import pytest
from depth_measure_seed import exec_, forget_identities  # noqa: F401
from observation_seed import ALICE, lite
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.public_store import MINIMUM, public_summary


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships({ALICE: {"lab": "member", "partner": "member"}})


async def _opt_in(owner_engine, tenant_id):
    await exec_(
        owner_engine,
        "UPDATE tenants SET public_totals = true WHERE id = :t",
        t=tenant_id,
    )


async def _wild(owner_engine, tenant_id, folder, n, *, start=0):
    """`n` wild fish on missions filed under `folder`."""
    for i in range(n):
        capture_id, _ = await lite(
            owner_engine, tenant_id, species=("Scarus coeruleus", "Blue Parrotfish"),
            device_name=f"{folder[:8]}-{start + i}",
        )  # fmt: skip
        await exec_(
            owner_engine,
            "UPDATE missions SET source_path = :p "
            "WHERE id = (SELECT mission_id FROM captures WHERE id = :c)",
            p=f"{folder}/{start + i}",
            c=capture_id,
        )


async def _summary(app_engine):
    # As the API reads it: the app role, no tenant in scope, nobody signed in.
    async with app_engine.connect() as conn:
        return await public_summary(conn)


async def test_a_tenant_that_has_not_opted_in_says_nothing(
    owner_engine, app_engine, tenants
):
    await _wild(owner_engine, tenants["lab"], "2024.06.20.REEF/062024_Alligator", 2)

    s = await _summary(app_engine)

    assert (s.lengths, s.wild_fish, s.species) == (0, 0, 0)
    assert s.regions == []


async def test_an_opted_in_tenant_counts_by_broad_region(
    owner_engine, app_engine, tenants
):
    lab = tenants["lab"]
    await _opt_in(owner_engine, lab)
    await _wild(owner_engine, lab, "SMILE/090123_SMILE Dives_Alligator", 3)
    await _wild(owner_engine, lab, "SMILE/CubaTrip2024_FSL06", 2, start=10)

    s = await _summary(app_engine)

    assert (s.lengths, s.wild_fish, s.species) == (5, 5, 1)
    # Islamorada and Cuba are not said: only their broad regions are.
    assert {r.name for r in s.regions} == {"Florida Keys, USA", "Caribbean"}
    assert all(r.count is None and r.under == MINIMUM for r in s.regions)


async def test_a_region_with_enough_fish_shows_a_rounded_count(
    owner_engine, app_engine, tenants
):
    lab = tenants["lab"]
    await _opt_in(owner_engine, lab)
    await _wild(owner_engine, lab, "SMILE/Molasses", MINIMUM + 2)

    (region,) = (await _summary(app_engine)).regions

    assert region.name == "Florida Keys, USA"
    assert (region.count, region.under) == (30, None)  # 27, to the nearest 10


async def test_fish_models_count_in_the_totals_not_on_the_map(
    owner_engine, app_engine, tenants
):
    lab = tenants["lab"]
    await _opt_in(owner_engine, lab)
    capture_id, _ = await lite(
        owner_engine, lab, model=("George", 0.3127), device_name="FSL-02"
    )
    # Filed where a wild fish would be on the map: a model still is not.
    await exec_(
        owner_engine,
        "UPDATE missions SET source_path = 'SMILE/Molasses/george' "
        "WHERE id = (SELECT mission_id FROM captures WHERE id = :c)",
        c=capture_id,
    )

    s = await _summary(app_engine)

    assert (s.lengths, s.wild_fish, s.model_lengths) == (1, 0, 1)
    assert s.regions == []


async def test_another_tenants_fish_stay_out_until_it_opts_in(
    owner_engine, app_engine, tenants
):
    await _opt_in(owner_engine, tenants["lab"])
    await _wild(owner_engine, tenants["lab"], "SMILE/Molasses", 1)
    await _wild(owner_engine, tenants["partner"], "SMILE/Molasses", 4, start=10)

    assert (await _summary(app_engine)).wild_fish == 1


async def test_the_app_role_reads_only_aggregates(owner_engine, app_engine, tenants):
    """The functions run as the owner, so they see past RLS; nothing else does."""
    await _opt_in(owner_engine, tenants["lab"])
    await _wild(owner_engine, tenants["lab"], "SMILE/Molasses", 1)

    async with app_engine.connect() as conn:
        observations = (
            await conn.execute(text("SELECT count(*) FROM observations"))
        ).scalar_one()

    assert observations == 0


# -- the route ------------------------------------------------------------------


@pytest.fixture
async def anonymous(app_engine):
    import httpx

    from fishsense_services_api.app import create_app
    from fishsense_services_api.auth import StaticKeySource, TokenValidator

    # No token is ever checked here: the route takes no caller.
    validator = TokenValidator(
        issuer="https://idp.test", audiences=("api",), keys=StaticKeySource({})
    )
    app = create_app(engine=app_engine, validator=validator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


async def test_anyone_reads_the_public_summary_without_signing_in(
    anonymous, owner_engine, tenants
):
    await _opt_in(owner_engine, tenants["lab"])
    await _wild(owner_engine, tenants["lab"], "SMILE/Molasses", 2)

    response = await anonymous.get("/public/summary")

    assert response.status_code == 200
    body = response.json()
    assert (body["wild_fish"], body["minimum"]) == (2, MINIMUM)
    assert body["regions"] == [
        {"name": "Florida Keys, USA", "latitude": 27.0, "longitude": -81.5,
         "count": None, "under": MINIMUM}
    ]  # fmt: skip
    # Nothing finer than a region, and no tenant, ever.
    assert "lab" not in response.text and "Molasses" not in response.text
