"""The database side of stage 1 (dive-frame clustering), tenant-scoped.

The cohort tests are ported from fishsense-lite@a8b2c3bc
services/fishsense-api/tests/test_select_next_dive_endpoints.py (the
dive-frame-clustering half); names, fixtures' shapes and reasons are v1's. The
cohort is v1's:

* the dive is high priority;
* some *canonical* capture carries a valid laser label -- completed, not
  superseded, x and y both set;
* the dive has no prediction cluster (label-studio clusters don't count).

v2 changes, each pinned here:

* the cohort is per tenant, ordered by `created_at` so the orchestrator can
  pick the oldest candidate across the tenants it serves;
* **persisting a dive's clusters is all-or-nothing.** v1 posted them one at a
  time, so a failure mid-persist left a partial set that its cohort gate then
  skipped forever ("a poison pill" -- v1's test, below, adapted) until an
  operator deleted the rows by hand;
* **the processor's output is not trusted** (PLAN.md §9.11): a capture that is
  not a canonical capture of the dive is refused, and nothing is written.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from fishsense_services_api.clustering_store import (
    ClusteringCatalog,
    ForeignCapture,
    InvalidClusters,
    canonical_capture_times,
    next_dive_for_clustering,
    persist_prediction_clusters,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import (
    create_dive,
    finalize_dive,
    register_capture,
)

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _dive(app_engine, tenant, path, *, priority="high") -> uuid.UUID:
    async with tenant_transaction(app_engine, tenant) as conn:
        dive = await create_dive(conn, tenant, source_path=path, name=path, dived_at=T0)
        await finalize_dive(conn, tenant, dive, priority=priority, dived_at=T0)
    return dive


async def _capture(app_engine, tenant, dive, name, *, checksum=None, at=T0):
    async with tenant_transaction(app_engine, tenant) as conn:
        registered = await register_capture(
            conn, tenant, dive_id=dive, device_id=None,
            source_path=f"{dive}/{name}", captured_at=at,
            checksum=checksum or uuid.uuid4().hex,
        )  # fmt: skip
    return registered.capture_id


async def _laser(owner_engine, tenant, capture, *, completed=True,
                 superseded=False, x=100.0, y=200.0):  # fmt: skip
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_labels (tenant_id, capture_id, source, "
                "ls_project_id, completed, superseded, x, y) "
                "VALUES (:t, :c, 'human', 43, :done, :gone, :x, :y)"
            ),
            {"t": tenant, "c": capture, "done": completed, "gone": superseded,
             "x": x, "y": y},
        )  # fmt: skip


async def _cluster(owner_engine, tenant, dive, formed_by, captures=()):
    async with owner_engine.begin() as conn:
        cluster = (
            await conn.execute(
                text(
                    "INSERT INTO dive_frame_clusters (tenant_id, dive_id, formed_by) "
                    "VALUES (:t, :d, :f) RETURNING id"
                ),
                {"t": tenant, "d": dive, "f": formed_by},
            )
        ).scalar_one()
        for capture in captures:
            await conn.execute(
                text(
                    "INSERT INTO dive_frame_cluster_captures "
                    "(tenant_id, cluster_id, capture_id) VALUES (:t, :k, :c)"
                ),
                {"t": tenant, "k": cluster, "c": capture},
            )


async def _next(app_engine, tenant):
    async with tenant_transaction(app_engine, tenant) as conn:
        candidate = await next_dive_for_clustering(conn, tenant)
    return None if candidate is None else candidate.dive_id


async def _clusters(owner_engine, dive) -> list[set[uuid.UUID]]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT k.id, array_agg(m.capture_id) AS captures "
                "FROM dive_frame_clusters k JOIN dive_frame_cluster_captures m "
                "ON m.cluster_id = k.id WHERE k.dive_id = :d "
                "AND k.formed_by = 'prediction' GROUP BY k.id"
            ),
            {"d": dive},
        )
        return [set(r.captures) for r in rows]


# -- the cohort (v1's tests) -----------------------------------------------------


async def test_clustering_requires_valid_laser_and_no_prediction_cluster(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    # dive 1: valid laser + already has PREDICTION cluster -> excluded.
    # dive 2: valid laser + no clusters -> picked.
    # dive 3: no valid laser -> excluded.
    d1, d2, d3 = [await _dive(app_engine, lab, f"d{i}") for i in (1, 2, 3)]
    for dive, completed in ((d1, True), (d2, True), (d3, False)):
        capture = await _capture(app_engine, lab, dive, "P1.ORF")
        await _laser(owner_engine, lab, capture, completed=completed)
    await _cluster(owner_engine, lab, d1, "prediction")

    assert await _next(app_engine, lab) == d2


async def test_clustering_excludes_dive_with_only_label_studio_cluster(
    owner_engine, app_engine
):
    """LABEL_STUDIO clusters come from stage 6.1 (label-time grouping)
    and don't count as PREDICTION clusters from stage 1. A dive whose
    only clusters are LABEL_STUDIO must still be picked for stage 1."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    await _laser(owner_engine, lab, await _capture(app_engine, lab, dive, "P1.ORF"))
    await _cluster(owner_engine, lab, dive, "label_studio")

    assert await _next(app_engine, lab) == dive


@pytest.mark.parametrize(
    "laser",
    [
        {"completed": False},
        {"superseded": True},
        {"x": None},
        {"y": None},
    ],
    ids=["incomplete", "superseded", "no-x", "no-y"],
)
async def test_clustering_excludes_incomplete_or_superseded_or_null_xy_lasers(
    owner_engine, app_engine, laser
):
    """Same gate as headtail: laser must be completed AND
    not superseded AND have both x and y populated to count as
    'valid laser', because that's what calibration and the validator
    treat as usable."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "P1.ORF")
    await _laser(owner_engine, lab, capture, **laser)

    assert await _next(app_engine, lab) is None


async def test_clustering_returns_none_with_no_high_priority(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1", priority="low")
    await _laser(owner_engine, lab, await _capture(app_engine, lab, dive, "P1.ORF"))

    assert await _next(app_engine, lab) is None


async def test_a_valid_laser_on_a_non_canonical_copy_does_not_count(
    owner_engine, app_engine
):
    """v1's predicate joins canonical images only: the duplicate copy of a frame
    is invisible to every cohort."""
    lab = await _tenant(owner_engine)
    original = await _dive(app_engine, lab, "original", priority="low")
    await _capture(app_engine, lab, original, "P1.ORF", checksum="c" * 32)
    copy = await _dive(app_engine, lab, "copy")
    duplicate = await _capture(app_engine, lab, copy, "P1.ORF", checksum="c" * 32)
    await _laser(owner_engine, lab, duplicate)

    assert await _next(app_engine, lab) is None


async def test_clustering_gate_is_any_prediction_cluster(owner_engine, app_engine):
    """v1's `test_clustering_partial_persist_is_a_poison_pill`, adapted. The
    gate stays "any prediction cluster" -- pinned so a "smarter" predicate
    doesn't change the story silently. v2 makes the partial state itself
    impossible: persist is all-or-nothing (see below), so the only way to
    have one prediction cluster is to have them all."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    captures = [await _capture(app_engine, lab, dive, f"P{i}.ORF") for i in range(3)]
    for capture in captures:
        await _laser(owner_engine, lab, capture)
    await _cluster(owner_engine, lab, dive, "prediction", captures[:1])

    assert await _next(app_engine, lab) is None


async def test_the_oldest_candidate_comes_first(owner_engine, app_engine):
    """v1 ordered by id -- first in, first out. v2 ids are UUIDs, so the order
    is `created_at`, which the orchestrator also compares across tenants."""
    lab = await _tenant(owner_engine)
    first = await _dive(app_engine, lab, "first")
    second = await _dive(app_engine, lab, "second")
    for dive in (second, first):
        await _laser(owner_engine, lab, await _capture(app_engine, lab, dive, "P.ORF"))

    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_clustering(conn, lab)

    assert candidate.dive_id == first
    assert candidate.created_at is not None


async def test_another_tenants_dive_is_never_a_candidate(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    theirs = await _dive(app_engine, partner, "d1")
    await _laser(
        owner_engine, partner, await _capture(app_engine, partner, theirs, "P.ORF")
    )

    assert await _next(app_engine, lab) is None


# -- the inputs (v1's resolver) --------------------------------------------------


async def test_the_inputs_are_the_dives_canonical_captures_and_their_times(
    owner_engine, app_engine
):
    """Canonical frames only: the same physical frames live under several dives,
    and the resolver must mirror the cohort predicate exactly, or the dispatched
    work would not match what the cohort promised and the dive could never
    drain (v1's resolver)."""
    lab = await _tenant(owner_engine)
    elsewhere = await _dive(app_engine, lab, "elsewhere", priority="low")
    await _capture(app_engine, lab, elsewhere, "X.ORF", checksum="d" * 32)
    dive = await _dive(app_engine, lab, "d1")
    late = await _capture(app_engine, lab, dive, "B.ORF", at=T0 + timedelta(seconds=9))
    early = await _capture(app_engine, lab, dive, "A.ORF", at=T0)
    await _capture(app_engine, lab, dive, "X.ORF", checksum="d" * 32)  # a copy

    async with tenant_transaction(app_engine, lab) as conn:
        inputs = await canonical_capture_times(conn, lab, dive)

    assert inputs == [(early, T0), (late, T0 + timedelta(seconds=9))]


# -- persisting (v1's persist, made atomic and checked) --------------------------


async def test_persists_one_prediction_cluster_per_group(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b, c = [await _capture(app_engine, lab, dive, f"{n}.ORF") for n in "ABC"]

    async with tenant_transaction(app_engine, lab) as conn:
        written = await persist_prediction_clusters(conn, lab, dive, [[a, b], [c], []])

    assert written == 2  # v1 skipped empty groups too
    assert sorted(await _clusters(owner_engine, dive), key=len) == [{c}, {a, b}]


async def test_a_capture_outside_the_dive_is_refused_and_nothing_is_written(
    owner_engine, app_engine
):
    """The processor runs on infrastructure we don't own (PLAN.md §9.11). A
    capture id that is not a canonical capture of this dive -- another dive's,
    a duplicate copy, or made up -- is refused, and because persist is one
    transaction, the groups before it are not written either."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    other = await _dive(app_engine, lab, "d2")
    mine = await _capture(app_engine, lab, dive, "A.ORF")
    theirs = await _capture(app_engine, lab, other, "B.ORF")

    for foreign in (theirs, uuid.uuid4()):
        with pytest.raises(ForeignCapture):
            async with tenant_transaction(app_engine, lab) as conn:
                await persist_prediction_clusters(conn, lab, dive, [[mine], [foreign]])

    assert await _clusters(owner_engine, dive) == []


async def test_persisting_again_writes_nothing(owner_engine, app_engine):
    """A retry after a commit whose acknowledgement was lost must not double
    the clusters. Clustering is one-shot per dive (the cohort gate), so a dive
    that already has prediction clusters is left as it is."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a = await _capture(app_engine, lab, dive, "A.ORF")

    for _ in range(2):
        async with tenant_transaction(app_engine, lab) as conn:
            written = await persist_prediction_clusters(conn, lab, dive, [[a]])

    assert written == 0
    assert await _clusters(owner_engine, dive) == [{a}]


# -- the catalog, as the orchestrator's principal --------------------------------


async def test_the_catalog_serves_only_the_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member", "reef": "member"},
         "someone-else": {"partner": "owner"}}
    )  # fmt: skip
    catalog = ClusteringCatalog(app_engine, sub=ORCHESTRATOR)

    assert set(await catalog.member_tenants()) == {tenants["lab"], tenants["reef"]}


async def test_the_catalog_runs_stage_1_within_a_tenant(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "A.ORF")
    await _laser(owner_engine, lab, capture)
    catalog = ClusteringCatalog(app_engine, sub=ORCHESTRATOR)

    candidate = await catalog.next_dive_for_clustering(lab)
    inputs = await catalog.canonical_capture_times(lab, dive)
    written = await catalog.persist_prediction_clusters(lab, dive, [[capture]])

    assert candidate.dive_id == dive
    assert inputs == [(capture, T0)]
    assert written == 1
    assert await catalog.next_dive_for_clustering(lab) is None


async def test_a_capture_in_two_clusters_is_refused(owner_engine, app_engine):
    """The kernel partitions the frames; output that doesn't is not the
    kernel's, and is refused rather than double-counted in stage 2."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b = [await _capture(app_engine, lab, dive, f"{n}.ORF") for n in "AB"]

    with pytest.raises(InvalidClusters):
        async with tenant_transaction(app_engine, lab) as conn:
            await persist_prediction_clusters(conn, lab, dive, [[a, b], [b]])

    assert await _clusters(owner_engine, dive) == []
