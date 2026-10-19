"""The mission-facts backfill: ports, deployments and purposes for what was
ingested before 0047 (PLAN.md §4.9, Phase 2b).

A plan is read-only and carries a fingerprint; applying needs the fingerprint
of the plan that was reviewed, writes under guards on the exact old values,
and writes nothing when anything moved since the review.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mission_backfill import (
    PlanChanged,
    apply_backfill,
    plan_backfill,
)

RULE = datetime(2026, 10, 16, tzinfo=timezone.utc)
BEFORE = datetime(2026, 10, 1, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 17, tzinfo=timezone.utc)
SAN_DIEGO = "/fishsense_data/2025.01.17.FishSense.San Diego"


async def _tenant(owner_engine, slug: str) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _mission(
    owner_engine, tenant_id, path, *, name, created_at=BEFORE, started_at=None, **cols
):
    columns = {
        "tenant_id": tenant_id,
        "source_path": path,
        "name": name,
        "started_at": started_at or datetime(2025, 1, 17, 18, tzinfo=timezone.utc),
        "created_at": created_at,
        **cols,
    }
    names = ", ".join(columns)
    values = ", ".join(f":{c}" for c in columns)
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    f"INSERT INTO missions ({names}) VALUES ({values}) RETURNING number"
                ),
                columns,
            )
        ).scalar_one()


async def _facts(owner_engine, number):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("""
                    SELECT m.port, m.port_source, m.port_note, m.purpose,
                           d.name AS deployment
                    FROM missions m LEFT JOIN deployments d ON d.id = m.deployment_id
                    WHERE m.number = :n
                    """),
                {"n": number},
            )
        ).one()


async def _plan(app_engine, tenant_id, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await plan_backfill(conn, tenant_id, ingested_before=RULE, **kwargs)


async def _apply(app_engine, tenant_id, fingerprint, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await apply_backfill(
            conn, tenant_id, ingested_before=RULE, expect=fingerprint, **kwargs
        )


def _by_number(plan):
    return {c.number: c for c in plan.missions}


async def test_what_was_ingested_before_the_rule_is_the_corrective_optic(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    old = await _mission(owner_engine, lab, "dump/080123_FSL-01 Photos", name="a")
    new = await _mission(
        owner_engine, lab, "upload/FSL-03/x", name="b", created_at=AFTER
    )
    labelled = await _mission(
        owner_engine, lab, f"{SAN_DIEGO}/ED-00/FSL-10D/Box", name="c", created_at=AFTER
    )

    changes = _by_number(await _plan(app_engine, lab))

    assert (changes[old].port, changes[old].port_source) == (
        "corrective_optic",
        "backfill",
    )
    # A D-labelled device says so whenever it was ingested; that is the stronger
    # evidence, so it is the source even for a mission the rule also covers.
    assert (changes[labelled].port, changes[labelled].port_source) == (
        "corrective_optic",
        "device_suffix",
    )
    # Ingested after the rule, with no D: nothing is known.
    assert changes[new].port is None


async def test_a_port_already_stated_is_left_alone(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    stated = await _mission(
        owner_engine,
        lab,
        "pool/flat-test",
        name="flat test",
        port="flat",
        port_source="operator",
        purpose="other",
    )

    plan = await _plan(app_engine, lab)

    assert stated not in _by_number(plan)


async def test_dataset_folders_become_deployments(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    first = await _mission(
        owner_engine,
        lab,
        f"{SAN_DIEGO}/ED-00/FSL-10D/Box",
        name="ED-00 FSL-10D Box",
        # 17 January in San Diego, the 18th in UTC.
        started_at=datetime(2025, 1, 18, 1, tzinfo=timezone.utc),
    )
    second = await _mission(
        owner_engine,
        lab,
        f"{SAN_DIEGO}/ED-01/FSL-11D/LensCalibration",
        name="ED-01 FSL-11D LensCalibration",
        started_at=datetime(2025, 1, 19, 2, tzinfo=timezone.utc),
    )
    loose = await _mission(owner_engine, lab, "dump/Alligator", name="Alligator")

    plan = await _plan(app_engine, lab)

    [trip] = plan.deployments
    assert (trip.name, trip.source_path, trip.exists) == (
        "2025.01.17.FishSense.San Diego",
        SAN_DIEGO,
        False,
    )
    # The dataset's name dates its start; mission times are UTC and the
    # site's zone is not recorded, so no end date is made up from them.
    assert (str(trip.started_on), trip.ended_on) == ("2025-01-17", None)
    changes = _by_number(plan)
    assert changes[first].deployment == changes[second].deployment == trip.name
    assert changes[loose].deployment is None


async def test_purposes_are_inferred_only_where_unstated_and_can_be_overridden(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine, "lab")
    laser = await _mission(owner_engine, lab, "p/LaserCalibration", name="LaserCal")
    ambiguous = await _mission(
        owner_engine, lab, "p/LaserCalibration Box", name="LaserCalibration Box"
    )
    reef = await _mission(owner_engine, lab, "d/Alligator", name="Alligator")

    plan = await _plan(
        app_engine, lab, purpose_overrides={ambiguous: "laser_calibration", reef: None}
    )

    changes = _by_number(plan)
    assert changes[laser].purpose == "laser_calibration"
    assert (changes[ambiguous].purpose, changes[ambiguous].purpose_why) == (
        "laser_calibration",
        "set by the operator",
    )
    assert changes[reef].purpose is None


async def test_planning_writes_nothing(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    number = await _mission(
        owner_engine, lab, f"{SAN_DIEGO}/ED-00/FSL-10D/Box", name="Box"
    )

    await _plan(app_engine, lab)

    facts = await _facts(owner_engine, number)
    assert (facts.port, facts.purpose, facts.deployment) == ("unknown", None, None)


async def test_applying_the_reviewed_plan_writes_it_once(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    number = await _mission(
        owner_engine, lab, f"{SAN_DIEGO}/ED-00/FSL-10D/Box", name="Box"
    )
    plan = await _plan(app_engine, lab)

    applied = await _apply(app_engine, lab, plan.fingerprint)

    assert (applied.missions, applied.deployments) == (1, 1)
    facts = await _facts(owner_engine, number)
    assert (facts.port, facts.port_source, facts.purpose, facts.deployment) == (
        "corrective_optic",
        "device_suffix",
        "known_length",
        "2025.01.17.FishSense.San Diego",
    )
    assert "FSL-10D" in facts.port_note
    again = await _plan(app_engine, lab)
    assert again.missions == () and again.deployments == ()


async def test_a_plan_that_moved_since_review_writes_nothing(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reviewed = await _mission(owner_engine, lab, "d/Alligator", name="Alligator")
    plan = await _plan(app_engine, lab)
    # Someone states the port between the review and the apply.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE missions SET port = 'flat', port_source = 'operator' "
                "WHERE number = :n"
            ),
            {"n": reviewed},
        )
    await _mission(owner_engine, lab, "d/Chutes", name="Chutes")

    with pytest.raises(PlanChanged):
        await _apply(app_engine, lab, plan.fingerprint)

    async with owner_engine.connect() as conn:
        purposes = (await conn.execute(text("SELECT purpose FROM missions"))).scalars()
        assert set(purposes) == {None}


async def test_the_overrides_are_part_of_what_was_reviewed(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    reef = await _mission(owner_engine, lab, "d/Alligator", name="Alligator")
    plan = await _plan(app_engine, lab)

    with pytest.raises(PlanChanged):
        await _apply(app_engine, lab, plan.fingerprint, purpose_overrides={reef: None})


async def test_a_plan_sees_only_its_tenant(owner_engine, app_engine):
    lab = await _tenant(owner_engine, "lab")
    partner = await _tenant(owner_engine, "partner")
    await _mission(owner_engine, partner, f"{SAN_DIEGO}/ED-00/FSL-10D/Box", name="Box")

    plan = await _plan(app_engine, lab)

    assert plan.missions == () and plan.deployments == ()


async def test_an_override_of_a_stated_purpose_is_refused_not_ignored(
    owner_engine, app_engine
):
    """The backfill fills only what is unstated. An override naming a mission
    whose purpose is already stated would silently change nothing; it is
    refused, so the reviewed plan never claims a correction it won't make."""
    lab = await _tenant(owner_engine, "lab")
    stated = await _mission(owner_engine, lab, "d/Alligator", name="Alligator",
                            purpose="survey")  # fmt: skip

    with pytest.raises(
        ValueError, match=rf"already stated for mission {stated} \(survey\)"
    ):
        await _plan(app_engine, lab, purpose_overrides={stated: "known_length"})
