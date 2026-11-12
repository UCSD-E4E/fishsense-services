"""Every phone mission in a deployment (the owner, 2026-11-11: "everything
should be in a deployment").

A deployment is one field trip: one place, on dates at most two days apart
(the Lite rule, `deployment_inference`). A phone mission has no folder, so its
place is its ``site`` and its day the local day its name carries. As it is
made, it joins the deployment holding a phone mission at the same place within
two days -- any phone's, so an iPad and an iPhone on one boat are one trip --
or the deployment already named for its day and place; otherwise it starts
one, named like its mission. A mission with no place is a trip of its phone's
alone, named ``…FishSense.Mobile``, and a place found later renames it.

``deploy-phone-missions`` puts the phone missions made before this rule into
deployments the same way: a dry run by default, written only with the
fingerprint of the plan reviewed.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from test_phone_missions import T0, _capture, _commit, alice, tenants  # noqa: F401

from fishsense_services_api import cli
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.phone_deployments import (
    PlanChanged,
    apply_phone_deployments,
    plan_phone_deployments,
)


async def _deployment(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT d.id, d.name, d.started_on, d.ended_on
                    FROM captures c
                    JOIN missions m ON m.id = c.mission_id
                    LEFT JOIN deployments d ON d.id = m.deployment_id
                    WHERE c.id = :c
                    """),
                {"c": capture_id},
            )
        ).one()


async def _commit_at(app_engine, lab, alice, **kw):
    return (await _commit(app_engine, lab, _capture(alice, **kw))).capture_id


async def test_a_new_phone_mission_starts_a_deployment_named_like_it(
    tenants, alice, app_engine, owner_engine
):
    c = await _commit_at(app_engine, tenants["lab"], alice)

    found = await _deployment(owner_engine, c)

    assert found.name == "2026.10.11.FishSense.La Jolla Cove"
    assert (found.started_on, found.ended_on) == (date(2026, 10, 11),) * 2


async def test_another_phone_at_the_same_place_a_day_later_joins_it(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit_at(app_engine, tenants["lab"], alice)
    later = await _commit_at(
        app_engine, tenants["lab"], alice, at=T0 + timedelta(days=1), install="ipad"
    )

    a, b = [await _deployment(owner_engine, c) for c in (first, later)]

    assert b.id == a.id
    assert (b.started_on, b.ended_on) == (date(2026, 10, 11), date(2026, 10, 12))


async def test_the_same_place_three_days_later_is_another_trip(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit_at(app_engine, tenants["lab"], alice)
    later = await _commit_at(
        app_engine, tenants["lab"], alice, at=T0 + timedelta(days=3)
    )

    a, b = [await _deployment(owner_engine, c) for c in (first, later)]

    assert b.id != a.id and b.name == "2026.10.14.FishSense.La Jolla Cove"


async def test_a_late_sync_from_three_days_before_is_another_trip(
    tenants, alice, app_engine, owner_engine
):
    later = await _commit_at(
        app_engine, tenants["lab"], alice, at=T0 + timedelta(days=3)
    )
    first = await _commit_at(app_engine, tenants["lab"], alice)

    a, b = [await _deployment(owner_engine, c) for c in (first, later)]

    assert b.id != a.id and a.name == "2026.10.11.FishSense.La Jolla Cove"


async def test_another_place_the_same_day_is_another_trip(
    tenants, alice, app_engine, owner_engine
):
    first = await _commit_at(app_engine, tenants["lab"], alice)
    other = await _commit_at(
        app_engine, tenants["lab"], alice, install="ipad", place="Scripps Pier"
    )

    a, b = [await _deployment(owner_engine, c) for c in (first, other)]

    assert b.id != a.id and b.name == "2026.10.11.FishSense.Scripps Pier"


async def test_the_day_is_the_phones_local_day(
    tenants, alice, app_engine, owner_engine
):
    late_evening = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)  # 22:00 PDT on 10-11
    c = await _commit_at(app_engine, tenants["lab"], alice, at=late_evening,
                         manifest={"time_zone": "America/Los_Angeles"})  # fmt: skip

    found = await _deployment(owner_engine, c)

    assert found.started_on == date(2026, 10, 11)
    assert found.name == "2026.10.11.FishSense.La Jolla Cove"


async def test_a_deployment_already_named_for_the_day_and_place_is_joined(
    tenants, alice, app_engine, owner_engine
):
    async with owner_engine.begin() as conn:
        existing = (
            await conn.execute(
                text("""
                    INSERT INTO deployments (tenant_id, name, started_on, ended_on)
                    VALUES (:t, '2026.10.11.FishSense.La Jolla Cove',
                            '2026-10-11', '2026-10-11')
                    RETURNING id
                    """),
                {"t": tenants["lab"]},
            )
        ).scalar_one()
    c = await _commit_at(app_engine, tenants["lab"], alice)

    assert (await _deployment(owner_engine, c)).id == existing


async def test_without_a_place_each_phone_is_its_own_trip(
    tenants, alice, app_engine, owner_engine
):
    mine = await _commit_at(app_engine, tenants["lab"], alice, place=None)
    theirs = await _commit_at(
        app_engine, tenants["lab"], alice, place=None, install="ipad"
    )
    again = await _commit_at(
        app_engine, tenants["lab"], alice, place=None, at=T0 + timedelta(days=1)
    )

    a, b, c = [await _deployment(owner_engine, x) for x in (mine, theirs, again)]

    assert a.name == "2026.10.11.FishSense.Mobile"
    assert b.id != a.id and b.name == "2026.10.11.FishSense.Mobile 2"
    assert c.id == a.id  # the same phone, the next day


async def test_a_derived_place_renames_a_phone_only_deployment(
    tenants, alice, app_engine, owner_engine
):
    from fishsense_services_api.place_name_store import record_place_name
    from fishsense_services_api.place_names import ReverseGeocode

    c = await _commit_at(app_engine, tenants["lab"], alice, place=None)
    async with tenant_transaction(app_engine, tenants["lab"]) as conn:
        await record_place_name(
            conn,
            tenants["lab"],
            c,
            ReverseGeocode(place_name="La Jolla Shores", source="test", properties={}),
        )

    assert (await _deployment(owner_engine, c)).name == (
        "2026.10.11.FishSense.La Jolla Shores"
    )


async def _undeploy_all(owner_engine) -> None:
    """Phone missions as they were before this rule: in no deployment."""
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE missions SET deployment_id = NULL WHERE source = 'mobile'")
        )
        await conn.execute(text("DELETE FROM deployments"))


async def _deployments(owner_engine) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM deployments"))
        ).scalar_one()


async def test_the_backfill_plans_without_writing_then_writes_the_reviewed_plan(
    tenants, alice, app_engine, owner_engine
):
    lab = tenants["lab"]
    first = await _commit_at(app_engine, lab, alice)
    ipad = await _commit_at(
        app_engine, lab, alice, install="ipad", at=T0 + timedelta(days=1)
    )
    pier = await _commit_at(app_engine, lab, alice, place="Scripps Pier",
                            at=T0 + timedelta(hours=5))  # fmt: skip
    await _undeploy_all(owner_engine)

    async with owner_engine.begin() as conn:
        plan = await plan_phone_deployments(conn, lab)
    assert await _deployments(owner_engine) == 0  # a plan writes nothing
    assert [(t.name, t.missions_count) for t in plan.trips] == [
        ("2026.10.11.FishSense.La Jolla Cove", 2),
        ("2026.10.11.FishSense.Scripps Pier", 1),
    ]

    async with owner_engine.begin() as conn:
        applied = await apply_phone_deployments(conn, lab, expect=plan.fingerprint)

    assert (applied.missions, applied.deployments) == (3, 2)
    a, b, c = [await _deployment(owner_engine, x) for x in (first, ipad, pier)]
    assert a.id == b.id and c.id != a.id


async def test_the_backfill_writes_nothing_for_another_plan(
    tenants, alice, app_engine, owner_engine
):
    await _commit_at(app_engine, tenants["lab"], alice)
    await _undeploy_all(owner_engine)

    with pytest.raises(PlanChanged):
        async with owner_engine.begin() as conn:
            await apply_phone_deployments(conn, tenants["lab"], expect="0" * 16)

    assert await _deployments(owner_engine) == 0


async def test_the_backfill_leaves_lite_missions_alone(
    tenants, alice, app_engine, owner_engine
):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO missions (tenant_id, source, source_path, name,
                                      started_at)
                VALUES (:t, 'lite', 'NAS/030426_Cove', 'a Lite dive', :at)
                """),
            {"t": tenants["lab"], "at": T0},
        )
    async with owner_engine.begin() as conn:
        plan = await plan_phone_deployments(conn, tenants["lab"])

    assert plan.trips == ()


@pytest.fixture
def as_owner(owner_url, monkeypatch) -> None:
    monkeypatch.setenv("FISHSENSE_MIGRATION_DATABASE_URL", owner_url)


async def test_the_cli_is_a_dry_run_until_given_the_fingerprint(
    tenants, alice, app_engine, owner_engine, as_owner, capsys
):
    await _commit_at(app_engine, tenants["lab"], alice)
    await _undeploy_all(owner_engine)
    async with owner_engine.connect() as conn:
        slug = (
            await conn.execute(
                text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenants["lab"]}
            )
        ).scalar_one()

    assert await cli.main(["deploy-phone-missions", "--tenant", slug]) == 0
    dry = capsys.readouterr().out
    assert "DRY RUN" in dry and "2026.10.11.FishSense.La Jolla Cove" in dry
    assert await _deployments(owner_engine) == 0

    fingerprint = dry.rsplit("--apply ", 1)[1].split()[0]
    assert (
        await cli.main(
            ["deploy-phone-missions", "--tenant", slug, "--apply", fingerprint]
        )
        == 0
    )
    assert await _deployments(owner_engine) == 1
    assert (
        await cli.main(
            ["deploy-phone-missions", "--tenant", slug, "--apply", fingerprint]
        )
        == 1
    )  # applied already: the plan is now another


async def test_the_cli_refuses_an_unknown_tenant(as_owner, tenants):
    assert await cli.main(["deploy-phone-missions", "--tenant", "nowhere"]) == 1
