"""The database side of re-grouping a dive that stage 6.1 already grouped.

New in v2 (2026-10-14). v1 walked a dive's prediction clusters in no stated
order, so a labeller's "Part of previous group" joined whatever cluster the
database happened to return before it: on dive 341, frame 101302 joined two
Stoplight Parrotfish frames half an hour later instead of the Hogfish one
second earlier; on dive 465, frames 129275 and 129276 became two animals.
v2's stage 6.1 walks them in capture order, but refuses a dive that already
has label-studio clusters -- every migrated dive. This store is what the
orchestrator's regroup reads, and its one write.

Pinned here:

* the facts: the dive's label-studio clusters with members and fish, and
  every current server measurement of a real fish, by capture, with numbers
  an operator can read;
* the replace is all or nothing, under stage 6.1's per-dive lock, and only
  if the clusters are still the ones the plan was made from (a fingerprint);
* it removes only label-studio clusters, binds each new one to the fish the
  plan names (or a new fish of the named species), and a capture whose fish
  changed becomes measurement work again -- the measurement itself is
  append-only and untouched;
* the catalog acts only as a member.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from depth_measure_seed import (
    _one,
    calibrated_dive,
    capture,
    cluster,
    fish,
    measurable_capture,
    measurement,
    tenant,
)
from fishsense_services_api.clustering_store import InvalidClusters
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.measurement_store import measurement_work
from fishsense_services_api.regroup_store import (
    ClustersChanged,
    NewCluster,
    RegroupCatalog,
    clusters_fingerprint,
    regroup_facts,
    replace_label_studio_clusters,
)
from fishsense_services_api.service_principal import NotAMember

ORCHESTRATOR = "service:fishsense-orchestrator"
PARROTFISH = "Fish, Stoplight Parrotfish (Sparisoma viride)"


async def _species(owner_engine, scientific, common):
    return await _one(
        owner_engine, "SELECT ensure_species(:s, :c)", s=scientific, c=common
    )


async def _number(owner_engine, table, row_id):
    return await _one(
        owner_engine, f"SELECT number FROM {table} WHERE id = :i", i=row_id
    )


async def _in(app_engine, tenant_id, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await fn(conn, tenant_id, *args, **kwargs)


async def _label_studio_clusters(owner_engine, dive_id):
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("""
                SELECT k.fish_id, array_agg(m.capture_id ORDER BY m.capture_id) AS members
                FROM dive_frame_clusters k
                JOIN dive_frame_cluster_captures m ON m.cluster_id = k.id
                WHERE k.dive_id = :d AND k.formed_by = 'label_studio'
                GROUP BY k.id ORDER BY min(k.number)
                """),
            {"d": dive_id},
        )
        return [(r.fish_id, set(r.members)) for r in rows]


async def _dive_341(owner_engine):
    """Dive 341 in miniature: a hogfish (one frame, `hog_frame`), and the
    frame v1 wrongly bound to a parrotfish's animal (`stray`)."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    hogfish = await fish(
        owner_engine, lab,
        species=await _species(owner_engine, "Lachnolaimus maximus", "Hogfish"),
    )  # fmt: skip
    parrotfish = await fish(
        owner_engine, lab,
        species=await _species(owner_engine, "Sparisoma viride", "Stoplight Parrotfish"),
    )  # fmt: skip
    hog_frame = await measurable_capture(owner_engine, lab, dive_id, in_cluster=False)
    stray = await measurable_capture(owner_engine, lab, dive_id, in_cluster=False)
    parrot_frame = await measurable_capture(
        owner_engine, lab, dive_id, PARROTFISH, in_cluster=False
    )
    await cluster(owner_engine, lab, dive_id, [hog_frame], fish_id=hogfish)
    await cluster(owner_engine, lab, dive_id, [stray, parrot_frame], fish_id=parrotfish)
    for frame, animal in ((hog_frame, hogfish), (stray, parrotfish),
                          (parrot_frame, parrotfish)):  # fmt: skip
        await measurement(owner_engine, lab, frame, animal, calibration)
    return lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame


# -- the facts ------------------------------------------------------------------


async def test_facts_name_the_clusters_and_the_current_bindings(
    owner_engine, app_engine
):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    await cluster(owner_engine, lab, dive_id, [hog_frame], formed_by="prediction")

    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    assert facts.priority == "high"
    assert [(k.fish_id, set(k.members)) for k in facts.label_studio_clusters] == [
        (hogfish, {hog_frame}),
        (parrotfish, {stray, parrot_frame}),
    ]
    assert facts.prediction_clusters == [[hog_frame]]
    bound = {(m.capture_id, m.fish_id) for m in facts.measured}
    assert bound == {
        (hog_frame, hogfish), (stray, parrotfish), (parrot_frame, parrotfish)
    }  # fmt: skip
    by_capture = {m.capture_id: m for m in facts.measured}
    assert by_capture[stray].fish_number == await _number(
        owner_engine, "fish", parrotfish
    )
    assert by_capture[stray].capture_number == await _number(
        owner_engine, "captures", stray
    )
    assert by_capture[stray].fish_scientific_name == "Sparisoma viride"
    assert facts.fingerprint == clusters_fingerprint(facts.label_studio_clusters)
    assert facts.canonical == {hog_frame, stray, parrot_frame}
    assert facts.fish[parrotfish].number == await _number(
        owner_engine, "fish", parrotfish
    )
    assert facts.fish[hogfish].scientific_name == "Lachnolaimus maximus"


async def test_facts_know_a_cluster_fish_no_measurement_names(owner_engine, app_engine):
    lab, dive_id, *_rest = await _dive_341(owner_engine)
    unmeasured = await fish(
        owner_engine, lab,
        species=await _species(owner_engine, "Epinephelus striatus", "Nassau Grouper"),
    )  # fmt: skip
    duplicate = await capture(owner_engine, lab, dive_id, canonical=False)
    await cluster(owner_engine, lab, dive_id, [duplicate], fish_id=unmeasured)

    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    assert facts.fish[unmeasured].scientific_name == "Epinephelus striatus"
    assert duplicate not in facts.canonical


async def test_the_fingerprint_moves_with_any_change_to_the_clusters(
    owner_engine, app_engine
):
    lab, dive_id, *_rest = await _dive_341(owner_engine)
    before = (await _in(app_engine, lab, regroup_facts, dive_id)).fingerprint

    await cluster(
        owner_engine, lab, dive_id, [await capture(owner_engine, lab, dive_id)]
    )

    assert (await _in(app_engine, lab, regroup_facts, dive_id)).fingerprint != before


# -- the replace ----------------------------------------------------------------


async def test_replace_rebinds_and_the_moved_frame_becomes_work(
    owner_engine, app_engine
):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    prediction = await cluster(
        owner_engine, lab, dive_id, [hog_frame, stray, parrot_frame],
        formed_by="prediction",
    )  # fmt: skip
    facts = await _in(app_engine, lab, regroup_facts, dive_id)
    assert (await _in(app_engine, lab, measurement_work, dive_id)).captures == []

    bound = await _in(
        app_engine, lab, replace_label_studio_clusters, dive_id,
        [NewCluster([hog_frame, stray], fish_id=hogfish),
         NewCluster([parrot_frame], fish_id=parrotfish)],
        expected_fingerprint=facts.fingerprint,
    )  # fmt: skip

    assert bound == [hogfish, parrotfish]
    assert await _label_studio_clusters(owner_engine, dive_id) == [
        (hogfish, {hog_frame, stray}),
        (parrotfish, {parrot_frame}),
    ]
    assert await _one(
        owner_engine, "SELECT count(*) FROM dive_frame_clusters WHERE id = :k",
        k=prediction,
    ) == 1  # fmt: skip
    work = await _in(app_engine, lab, measurement_work, dive_id)
    assert [item.capture_id for item in work.captures] == [stray]
    assert await _one(
        owner_engine, "SELECT count(*) FROM measurements WHERE capture_id = :c",
        c=stray,
    ) == 1  # fmt: skip


async def test_replace_creates_a_fish_of_the_named_species(owner_engine, app_engine):
    """Dive 383: a Nassau Grouper frame bound to a hogfish's animal gets an
    animal of its own, of its own species."""
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    bound = await _in(
        app_engine, lab, replace_label_studio_clusters, dive_id,
        [NewCluster([hog_frame], fish_id=hogfish),
         NewCluster([stray], new_fish_names=("Nassau Grouper", "Epinephelus striatus")),
         NewCluster([parrot_frame], fish_id=parrotfish)],
        expected_fingerprint=facts.fingerprint,
    )  # fmt: skip

    new_fish = bound[1]
    assert new_fish not in (hogfish, parrotfish)
    assert (
        await _one(
            owner_engine,
            "SELECT s.scientific_name FROM fish f JOIN species s ON s.id = f.species_id "
            "WHERE f.id = :f",
            f=new_fish,
        )
        == "Epinephelus striatus"
    )
    work = await _in(app_engine, lab, measurement_work, dive_id)
    assert [item.capture_id for item in work.captures] == [stray]


async def test_a_cluster_with_no_measured_frame_is_left_unbound(
    owner_engine, app_engine
):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    unmeasured = await capture(owner_engine, lab, dive_id)
    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    bound = await _in(
        app_engine, lab, replace_label_studio_clusters, dive_id,
        [NewCluster([hog_frame], fish_id=hogfish),
         NewCluster([stray, parrot_frame], fish_id=parrotfish),
         NewCluster([unmeasured])],
        expected_fingerprint=facts.fingerprint,
    )  # fmt: skip

    assert bound == [hogfish, parrotfish, None]


async def test_replace_refuses_clusters_changed_since_the_plan(
    owner_engine, app_engine
):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    facts = await _in(app_engine, lab, regroup_facts, dive_id)
    await cluster(
        owner_engine, lab, dive_id, [await capture(owner_engine, lab, dive_id)]
    )
    before = await _label_studio_clusters(owner_engine, dive_id)

    with pytest.raises(ClustersChanged):
        await _in(
            app_engine, lab, replace_label_studio_clusters, dive_id,
            [NewCluster([hog_frame, stray, parrot_frame], fish_id=hogfish)],
            expected_fingerprint=facts.fingerprint,
        )  # fmt: skip

    assert await _label_studio_clusters(owner_engine, dive_id) == before


async def test_replace_refuses_a_capture_of_another_dive(owner_engine, app_engine):
    lab, dive_id, hogfish, *_rest = await _dive_341(owner_engine)
    other_dive, _ = await calibrated_dive(owner_engine, lab)
    foreign = await capture(owner_engine, lab, other_dive)
    facts = await _in(app_engine, lab, regroup_facts, dive_id)
    before = await _label_studio_clusters(owner_engine, dive_id)

    with pytest.raises(ValueError):
        await _in(
            app_engine, lab, replace_label_studio_clusters, dive_id,
            [NewCluster([foreign], fish_id=hogfish)],
            expected_fingerprint=facts.fingerprint,
        )  # fmt: skip

    assert await _label_studio_clusters(owner_engine, dive_id) == before


async def test_replace_refuses_a_capture_repeated_within_a_cluster(
    owner_engine, app_engine
):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    facts = await _in(app_engine, lab, regroup_facts, dive_id)
    before = await _label_studio_clusters(owner_engine, dive_id)

    with pytest.raises(InvalidClusters):
        await _in(
            app_engine, lab, replace_label_studio_clusters, dive_id,
            [NewCluster([hog_frame, stray, hog_frame], fish_id=hogfish),
             NewCluster([parrot_frame], fish_id=parrotfish)],
            expected_fingerprint=facts.fingerprint,
        )  # fmt: skip

    assert await _label_studio_clusters(owner_engine, dive_id) == before


async def test_replace_waits_for_stage_14s_lock_on_the_dive(owner_engine, app_engine):
    """Stage 14 reads a frame's cluster, then binds it under its
    `measure-fish` lock; a regroup must not delete the cluster in between."""
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    async with owner_engine.begin() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"measure-fish:{dive_id}"},
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                _in(
                    app_engine, lab, replace_label_studio_clusters, dive_id,
                    [NewCluster([hog_frame, stray], fish_id=hogfish),
                     NewCluster([parrot_frame], fish_id=parrotfish)],
                    expected_fingerprint=facts.fingerprint,
                ),
                timeout=1.0,
            )  # fmt: skip


async def test_replace_refuses_a_fish_of_another_tenant(owner_engine, app_engine):
    lab, dive_id, hogfish, parrotfish, hog_frame, stray, parrot_frame = await _dive_341(
        owner_engine
    )
    partner = await tenant(owner_engine, "partner")
    theirs = await fish(owner_engine, partner)
    facts = await _in(app_engine, lab, regroup_facts, dive_id)

    with pytest.raises(ValueError, match="not real fish"):
        await _in(
            app_engine, lab, replace_label_studio_clusters, dive_id,
            [NewCluster([hog_frame, stray, parrot_frame], fish_id=theirs)],
            expected_fingerprint=facts.fingerprint,
        )  # fmt: skip


# -- the catalog ----------------------------------------------------------------


async def test_the_catalog_acts_only_as_a_member(app_engine, seed_memberships):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member"}, "someone-else": {"partner": "owner"}}
    )
    catalog = RegroupCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [tenants["lab"]]
    with pytest.raises(NotAMember):
        await catalog.regroup_facts(tenants["partner"], uuid.uuid4())
    with pytest.raises(NotAMember):
        await catalog.replace_label_studio_clusters(
            tenants["partner"], uuid.uuid4(), [], expected_fingerprint=""
        )


async def test_the_catalog_finds_a_dive_by_number(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    catalog = RegroupCatalog(app_engine, sub=ORCHESTRATOR)

    assert (
        await catalog.dive_by_number(lab, await _number(owner_engine, "dives", dive_id))
        == dive_id
    )
