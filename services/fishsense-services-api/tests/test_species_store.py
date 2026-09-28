"""The database side of the species stages, tenant-scoped.

Ported from fishsense-lite@77e8f8e5:

* the cohorts: services/fishsense-api/tests/test_select_next_dive_endpoints.py
  (the species-preprocessing and needing-species-population tests; names,
  fixture shapes and reasons are v1's) and test_cohort_needs_reprocess_all_kinds.py
  (the species case);
* the flag: test_needs_reprocess_scoping.py and test_needs_reprocess_clear_scope.py
  (species);
* the reads the workers made through the SDK (get_species_labels,
  get_clusters, put_species_label, post_cluster, set_dive_slate,
  set_calibration_target, set_notes).

v2 changes, each pinned here:

* per tenant, ordered by `created_at` (v1: `id`) so the orchestrator can take
  the oldest candidate across the tenants it serves;
* **populate's candidates are canonical only**, mirroring its cohort. v1 read
  every laser-valid image of the dive; a duplicate frame shares its canonical
  twin's JPEG and task URL, so it would have been anchored to the twin's task;
* **cluster order is stated**: clusters by their earliest member's
  `captured_at` (then `number`), members by `captured_at`, `number`. v1's
  `GET clusters` had no ORDER BY, and both "image i of N" and stage 6.1's
  "Part of previous group" read the order;
* **stage 6.1 persists all or nothing**, serialised per dive (v1 posted one
  cluster at a time, so a failure left a partial set that blocked every re-run);
* **a refusal expires by comparison, not by clearing**: the sync stamps
  `dives.calibration_links_changed_at` when it writes a link, and a refused
  calibration row older than that stamp no longer stands (migration
  species_01). v1 nulled three refusal columns on `dive`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from fishsense_services_api.clustering_store import InvalidClusters
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import (
    create_dive,
    finalize_dive,
    register_capture,
)
from fishsense_services_api.species_store import (
    SpeciesCatalog,
    calibration_targets_by_name,
    dives_needing_species_population,
    next_dive_for_species_preprocessing,
    note_unidentified_slate,
    persist_label_studio_clusters,
    record_species_label,
    refusal_outlived_by_link_change,
    set_dive_calibration_target,
    set_dive_slate_template,
    set_species_needs_reprocess,
    slate_templates_by_name,
    species_grouping_facts,
    species_population_facts,
    species_preprocess_facts,
    supersede_species_labels,
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


async def _dive(app_engine, tenant, path, *, priority="high", device=None):
    async with tenant_transaction(app_engine, tenant) as conn:
        dive = await create_dive(
            conn, tenant, source_path=path, name=path, dived_at=T0, device_id=device
        )
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
                 superseded=False, x=100.0, y=200.0, project=43):  # fmt: skip
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_labels (tenant_id, capture_id, source, "
                "ls_project_id, completed, superseded, x, y) "
                "VALUES (:t, :c, 'human', :p, :done, :gone, :x, :y)"
            ),
            {"t": tenant, "c": capture, "done": completed, "gone": superseded,
             "x": x, "y": y, "p": project},
        )  # fmt: skip


async def _species(owner_engine, tenant, capture, *, project=70, task=None,
                   completed=False, superseded=False, needs_reprocess=False,
                   **columns):  # fmt: skip
    values = {"t": tenant, "c": capture, "p": project, "k": task,
              "done": completed, "gone": superseded, "flag": needs_reprocess,
              **columns}  # fmt: skip
    extra = "".join(f", {c}" for c in columns)
    extra_values = "".join(f", :{c}" for c in columns)
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO species_labels (tenant_id, capture_id, source, "
                    "ls_project_id, ls_task_id, completed, superseded, "
                    f"needs_reprocess{extra}) VALUES (:t, :c, 'human', :p, :k, "
                    f":done, :gone, :flag{extra_values}) RETURNING id"
                ),
                values,
            )
        ).scalar_one()


async def _cluster(owner_engine, tenant, dive, formed_by, captures=(), *, id=None):
    # pylint: disable=redefined-builtin
    async with owner_engine.begin() as conn:
        cluster = (
            await conn.execute(
                text(
                    "INSERT INTO dive_frame_clusters (id, tenant_id, dive_id, "
                    "formed_by) VALUES (coalesce(:i, gen_random_uuid()), :t, :d, :f) "
                    "RETURNING id"
                ),
                {"i": id, "t": tenant, "d": dive, "f": formed_by},
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
    return cluster


async def _row(owner_engine, table, row_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(f"SELECT * FROM {table} WHERE id = :i"), {"i": row_id}
            )
        ).one()


async def _next(app_engine, tenant):
    async with tenant_transaction(app_engine, tenant) as conn:
        candidate = await next_dive_for_species_preprocessing(conn, tenant)
    return None if candidate is None else candidate.dive_id


async def _population(app_engine, tenant):
    async with tenant_transaction(app_engine, tenant) as conn:
        return [c.dive_id for c in await dives_needing_species_population(conn, tenant)]


# -- stage 2: species-preprocessing (v1's tests) ----------------------------------


async def test_species_preprocessing_requires_prediction_cluster_and_valid_laser(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    # dive 1: no PREDICTION cluster -> excluded.
    # dive 2: PREDICTION cluster but no laser-valid image -> excluded.
    # dive 3: PREDICTION cluster + laser-valid image IN that cluster,
    #         without a species label -> picked.
    d1, d2, d3 = [await _dive(app_engine, lab, f"d{i}") for i in (1, 2, 3)]
    c1, c2, c3 = [await _capture(app_engine, lab, d, "P.ORF") for d in (d1, d2, d3)]
    await _cluster(owner_engine, lab, d2, "prediction", [c2])
    await _cluster(owner_engine, lab, d3, "prediction", [c3])
    await _laser(owner_engine, lab, c1)
    await _laser(owner_engine, lab, c2, completed=False)
    await _laser(owner_engine, lab, c3)

    assert await _next(app_engine, lab) == d3


async def test_species_preprocessing_excludes_dive_with_only_incomplete_species_labels(
    owner_engine, app_engine
):
    """Once populate seeds an incomplete species label (with a real project)
    for every laser-valid image, the dive drops out of the cohort."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    captures = [await _capture(app_engine, lab, dive, n) for n in ("a", "b")]
    await _cluster(owner_engine, lab, dive, "prediction", captures)
    for capture in captures:
        await _laser(owner_engine, lab, capture)
        await _species(owner_engine, lab, capture, project=70)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_excludes_dive_when_sentinel_coexists_with_real_label(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=None)
    await _species(owner_engine, lab, capture, project=70)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_ignores_null_project_species_sentinels(
    owner_engine, app_engine
):
    """Sentinel rows (no project) must NOT drop a dive from the cohort."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=None)

    assert await _next(app_engine, lab) == dive


async def test_species_preprocessing_excludes_incomplete_or_superseded_or_null_xy_lasers(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    shapes = [
        {"completed": False},
        {"superseded": True},
        {"x": None},
        {"y": None},
    ]
    for i, shape in enumerate(shapes):
        dive = await _dive(app_engine, lab, f"d{i}")
        capture = await _capture(app_engine, lab, dive, "a")
        await _cluster(owner_engine, lab, dive, "prediction", [capture])
        await _laser(owner_engine, lab, capture, **shape)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_returns_none_when_only_label_studio_clusters(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "label_studio", [capture])
    await _laser(owner_engine, lab, capture)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_reenters_dive_with_only_superseded_species(
    owner_engine, app_engine
):
    """A superseded real-project species row must NOT gate the image out."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=117, superseded=True)

    assert await _next(app_engine, lab) == dive


async def test_species_preprocessing_still_excludes_live_real_species_row(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=70)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_skips_qualifying_image_not_in_a_cluster(
    owner_engine, app_engine
):
    """The 2026-07-22 poison pill: a qualifying image that is NOT clustered
    must not select the dive (the resolver needs its cluster for i of N)."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    clustered = await _capture(app_engine, lab, dive, "a")
    loose = await _capture(app_engine, lab, dive, "b")
    await _cluster(owner_engine, lab, dive, "prediction", [clustered])
    await _laser(owner_engine, lab, clustered)
    await _laser(owner_engine, lab, loose)
    await _species(owner_engine, lab, clustered, project=70)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_picks_dive_with_a_clustered_qualifying_image(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)

    assert await _next(app_engine, lab) == dive


async def test_species_preprocessing_needs_high_priority(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1", priority="low")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)

    assert await _next(app_engine, lab) is None


async def test_species_preprocessing_is_canonical_only(owner_engine, app_engine):
    """A duplicate frame (its canonical copy is under another, low dive) must
    not select the dive: populate never tasks it, so it would never drain."""
    lab = await _tenant(owner_engine)
    original = await _dive(app_engine, lab, "orig", priority="low")
    await _capture(app_engine, lab, original, "a", checksum="a" * 32)
    duplicate = await _dive(app_engine, lab, "dup")
    copy = await _capture(app_engine, lab, duplicate, "a", checksum="a" * 32)
    await _cluster(owner_engine, lab, duplicate, "prediction", [copy])
    await _laser(owner_engine, lab, copy)

    assert await _next(app_engine, lab) is None


# the reprocess flag: the cohort's second way in (test_cohort_needs_reprocess_all_kinds)


async def test_a_flagged_species_label_selects_its_dive(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=70, completed=True,
                   needs_reprocess=True)  # fmt: skip

    assert await _next(app_engine, lab) == dive


async def test_a_flag_on_a_superseded_row_does_not_select(owner_engine, app_engine):
    """The resolver never sees a superseded row, so selecting on one picks a
    dive it finds no work for, every hour, forever."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _species(owner_engine, lab, capture, project=70, superseded=True,
                   needs_reprocess=True)  # fmt: skip

    assert await _next(app_engine, lab) is None


async def test_a_flag_on_a_duplicate_frame_does_not_select(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    original = await _dive(app_engine, lab, "orig", priority="low")
    await _capture(app_engine, lab, original, "a", checksum="b" * 32)
    duplicate = await _dive(app_engine, lab, "dup")
    copy = await _capture(app_engine, lab, duplicate, "a", checksum="b" * 32)
    await _species(owner_engine, lab, copy, project=70, needs_reprocess=True)

    assert await _next(app_engine, lab) is None


async def test_the_oldest_candidate_is_first(owner_engine, app_engine):
    """v2: first in, first out by `created_at` (v1: lowest id)."""
    lab = await _tenant(owner_engine)
    dives = []
    for name in ("first", "second"):
        dive = await _dive(app_engine, lab, name)
        capture = await _capture(app_engine, lab, dive, "a")
        await _cluster(owner_engine, lab, dive, "prediction", [capture])
        await _laser(owner_engine, lab, capture)
        dives.append(dive)

    assert await _next(app_engine, lab) == dives[0]


# -- species population (v1's tests) ------------------------------------------------


async def test_needing_species_population_picks_laser_valid_without_live_species(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    high = await _dive(app_engine, lab, "d1")
    low = await _dive(app_engine, lab, "d2", priority="low")
    # No PREDICTION cluster required (unlike species-preprocessing).
    await _laser(owner_engine, lab, await _capture(app_engine, lab, high, "a"))
    await _laser(owner_engine, lab, await _capture(app_engine, lab, low, "a"))

    assert await _population(app_engine, lab) == [high]


async def test_needing_species_population_reincludes_superseded_only_dive(
    owner_engine, app_engine
):
    """The migration case: both cohorts must re-include a dive whose only
    species rows are superseded, or they deadlock."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=117, superseded=True)

    assert await _population(app_engine, lab) == [dive]
    assert await _next(app_engine, lab) == dive


async def test_needing_species_population_excludes_dive_with_live_species(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _laser(owner_engine, lab, capture)
    await _species(owner_engine, lab, capture, project=226)

    assert await _population(app_engine, lab) == []


async def test_needing_species_population_excludes_invalid_laser(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    for name, shape in (("a", {"completed": False}), ("b", {"superseded": True}),
                        ("c", {"x": None})):  # fmt: skip
        await _laser(
            owner_engine, lab, await _capture(app_engine, lab, dive, name), **shape
        )

    assert await _population(app_engine, lab) == []


async def test_needing_species_population_lists_every_dive_oldest_first(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dives = [await _dive(app_engine, lab, f"d{i}") for i in range(3)]
    for dive in dives:
        await _laser(owner_engine, lab, await _capture(app_engine, lab, dive, "a"))

    assert await _population(app_engine, lab) == dives


# -- what the resolver reads -----------------------------------------------------------


async def _device_with_intrinsics(owner_engine, tenant, *matrices):
    async with owner_engine.begin() as conn:
        device = (
            await conn.execute(
                text(
                    "INSERT INTO devices (tenant_id, kind, serial, name) "
                    "VALUES (:t, 'lite', 'BHF12345', 'FSL-01') RETURNING id"
                ),
                {"t": tenant},
            )
        ).scalar_one()
        for matrix in matrices:
            await conn.execute(
                text(
                    "INSERT INTO camera_calibrations (tenant_id, device_id, "
                    "camera_matrix, distortion_coefficients) VALUES (:t, :d, "
                    "CAST(:m AS jsonb), CAST(:k AS jsonb))"
                ),
                {"t": tenant, "d": device, "m": matrix, "k": "[-0.1, 0.05, 0, 0, 0]"},
            )
    return device


K1 = "[[1000, 0, 960], [0, 1000, 540], [0, 0, 1]]"
K2 = "[[2000, 0, 960], [0, 2000, 540], [0, 0, 1]]"


async def test_preprocess_facts_carry_the_devices_current_intrinsics(
    owner_engine, app_engine
):
    """v1 read `dive.camera_id`'s intrinsics; v2's are per device and
    append-only, so the current (latest) calibration is the one."""
    lab = await _tenant(owner_engine)
    device = await _device_with_intrinsics(owner_engine, lab, K1, K2)
    dive = await _dive(app_engine, lab, "d1", device=device)

    async with tenant_transaction(app_engine, lab) as conn:
        facts = await species_preprocess_facts(conn, lab, dive)

    assert facts.device_id == device
    assert facts.intrinsics.camera_matrix[0][0] == 2000
    assert facts.intrinsics.distortion_coefficients == [-0.1, 0.05, 0, 0, 0]


async def test_preprocess_facts_without_a_device_or_intrinsics(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    bare = await _dive(app_engine, lab, "d1")
    device = await _device_with_intrinsics(owner_engine, lab)
    uncalibrated = await _dive(app_engine, lab, "d2", device=device)

    async with tenant_transaction(app_engine, lab) as conn:
        assert (await species_preprocess_facts(conn, lab, bare)).device_id is None
        facts = await species_preprocess_facts(conn, lab, uncalibrated)
        assert facts.device_id == device and facts.intrinsics is None
        assert await species_preprocess_facts(conn, lab, uuid.uuid4()) is None


async def test_preprocess_facts_order_clusters_and_members_by_capture_time(
    owner_engine, app_engine
):
    """v1's GET clusters had no ORDER BY. "Image i of N" and stage 6.1 both
    read the order, so v2 states it: clusters by their earliest member,
    members by `captured_at` then `number`."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    at = [T0 + timedelta(seconds=s) for s in range(6)]
    c = [await _capture(app_engine, lab, dive, f"{i}", at=at[i]) for i in range(6)]
    # Inserted late-first, members scrambled, and cluster ids neither in nor
    # against time order, so no incidental order can pass for the stated one.
    await _cluster(owner_engine, lab, dive, "prediction", [c[5]], id=uuid.UUID(int=1))
    await _cluster(owner_engine, lab, dive, "prediction", [c[4], c[3]],
                   id=uuid.UUID(int=0))  # fmt: skip
    await _cluster(owner_engine, lab, dive, "prediction", [c[2], c[0], c[1]],
                   id=uuid.UUID(int=2))  # fmt: skip
    await _cluster(owner_engine, lab, dive, "label_studio", [c[0]])

    async with tenant_transaction(app_engine, lab) as conn:
        facts = await species_preprocess_facts(conn, lab, dive)

    assert facts.prediction_clusters == [[c[0], c[1], c[2]], [c[3], c[4]], [c[5]]]
    assert [x.capture_id for x in facts.captures] == c


async def test_preprocess_facts_read_canonical_captures_lasers_and_live_species(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    other = await _dive(app_engine, lab, "other", priority="low")
    await _capture(app_engine, lab, other, "dup", checksum="c" * 32)
    dive = await _dive(app_engine, lab, "d1")
    valid = await _capture(app_engine, lab, dive, "a", checksum="a" * 32)
    invalid = await _capture(app_engine, lab, dive, "b")
    duplicate = await _capture(app_engine, lab, dive, "dup", checksum="c" * 32)
    await _laser(owner_engine, lab, valid)
    await _laser(owner_engine, lab, invalid, completed=False)
    live = await _species(owner_engine, lab, valid, project=70, needs_reprocess=True)
    await _species(owner_engine, lab, invalid, project=71, superseded=True)

    async with tenant_transaction(app_engine, lab) as conn:
        facts = await species_preprocess_facts(conn, lab, dive)

    assert {x.capture_id for x in facts.captures} == {valid, invalid}
    assert duplicate not in {x.capture_id for x in facts.captures}
    assert facts.captures[0].checksum == "a" * 32
    assert facts.captures[0].from_v1 is False
    assert facts.valid_laser == frozenset({valid})
    assert [(r.id, r.needs_reprocess) for r in facts.species_labels] == [(live, True)]


# -- the reprocess flag (test_needs_reprocess_scoping / _clear_scope, species) --------


async def _flagged(owner_engine, label_id) -> bool:
    return (await _row(owner_engine, "species_labels", label_id)).needs_reprocess


async def test_raising_flags_only_incomplete_live_canonical_labels(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b, c = [await _capture(app_engine, lab, dive, n) for n in "abc"]
    open_ = await _species(owner_engine, lab, a, project=70)
    done = await _species(owner_engine, lab, b, project=70, completed=True)
    gone = await _species(owner_engine, lab, c, project=70, superseded=True)

    async with tenant_transaction(app_engine, lab) as conn:
        raised = await set_species_needs_reprocess(conn, lab, dive, True)

    assert raised == 1
    assert await _flagged(owner_engine, open_)
    assert not await _flagged(owner_engine, done)
    assert not await _flagged(owner_engine, gone)

    async with tenant_transaction(app_engine, lab) as conn:
        assert (
            await set_species_needs_reprocess(
                conn, lab, dive, True, only_incomplete=False
            )
            == 2
        )
    assert await _flagged(owner_engine, done)


async def test_clearing_lowers_every_canonical_flag_regardless_of_state(
    owner_engine, app_engine
):
    """Clearing is wider on purpose: a row completed or superseded after
    being flagged must still come down, or it holds its dive forever."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b = [await _capture(app_engine, lab, dive, n) for n in "ab"]
    done = await _species(owner_engine, lab, a, completed=True, needs_reprocess=True)
    gone = await _species(owner_engine, lab, b, superseded=True, needs_reprocess=True)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await set_species_needs_reprocess(conn, lab, dive, False) == 2

    assert not await _flagged(owner_engine, done)
    assert not await _flagged(owner_engine, gone)


async def test_scoped_clear_leaves_a_flag_raised_during_the_run(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    redrawn, raised_mid_run = [await _capture(app_engine, lab, dive, n) for n in "ab"]
    first = await _species(owner_engine, lab, redrawn, needs_reprocess=True)
    second = await _species(owner_engine, lab, raised_mid_run, needs_reprocess=True)

    async with tenant_transaction(app_engine, lab) as conn:
        cleared = await set_species_needs_reprocess(
            conn, lab, dive, False, capture_ids=[redrawn]
        )

    assert cleared == 1
    assert not await _flagged(owner_engine, first)
    assert await _flagged(owner_engine, second)


async def test_an_empty_scope_is_not_read_as_no_scope(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    label = await _species(
        owner_engine, lab, await _capture(app_engine, lab, dive, "a"),
        needs_reprocess=True,
    )  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await set_species_needs_reprocess(
            conn, lab, dive, False, capture_ids=[]
        ) == 0  # fmt: skip

    assert await _flagged(owner_engine, label)


async def test_the_flag_never_touches_a_duplicate_frame(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    other = await _dive(app_engine, lab, "other", priority="low")
    await _capture(app_engine, lab, other, "a", checksum="d" * 32)
    dive = await _dive(app_engine, lab, "d1")
    copy = await _capture(app_engine, lab, dive, "a", checksum="d" * 32)
    label = await _species(owner_engine, lab, copy)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await set_species_needs_reprocess(conn, lab, dive, True) == 0

    assert not await _flagged(owner_engine, label)


# -- what populate reads and writes ---------------------------------------------------


async def test_population_facts_are_canonical_valid_laser_captures_in_capture_order(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    other = await _dive(app_engine, lab, "other", priority="low")
    await _capture(app_engine, lab, other, "dup", checksum="e" * 32)
    dive = await _dive(app_engine, lab, "d1")
    late = await _capture(app_engine, lab, dive, "late", at=T0 + timedelta(seconds=9))
    early = await _capture(app_engine, lab, dive, "early", at=T0)
    duplicate = await _capture(app_engine, lab, dive, "dup", checksum="e" * 32)
    invalid = await _capture(app_engine, lab, dive, "invalid")
    for capture in (late, early, duplicate):
        await _laser(owner_engine, lab, capture)
    # A second valid label must not list the capture twice.
    await _laser(owner_engine, lab, early, project=44)
    await _laser(owner_engine, lab, invalid, completed=False)
    sentinel = await _species(owner_engine, lab, early, project=None,
                              content_of_image="Fish, Hogfish (Lachnolaimus maximus)")  # fmt: skip
    await _species(owner_engine, lab, late, project=99, superseded=True)

    async with tenant_transaction(app_engine, lab) as conn:
        facts = await species_population_facts(conn, lab, dive)

    assert [c.capture_id for c in facts.candidates] == [early, late]
    assert [(r.id, r.ls_project_id) for r in facts.species_labels] == [(sentinel, None)]
    assert facts.species_labels[0].content_of_image.startswith("Fish, Hogfish")


async def test_recording_a_label_seeds_a_human_row_for_the_task(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")

    async with tenant_transaction(app_engine, lab) as conn:
        await record_species_label(
            conn, lab, capture_id=capture, ls_project_id=70, ls_task_id=5001,
            image_url="s3://labels/fishsense-lite/preprocess_groups_jpeg/a.JPG",
        )  # fmt: skip

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM species_labels WHERE capture_id = :c"),
                {"c": capture},
            )
        ).one()
    assert (row.source, row.ls_project_id, row.ls_task_id) == ("human", 70, 5001)
    assert row.image_url.endswith("preprocess_groups_jpeg/a.JPG")
    assert (row.completed, row.superseded) == (False, False)


async def test_recording_revives_the_projects_superseded_row_and_keeps_its_flag(
    owner_engine, app_engine
):
    """v1's natural-key upsert on (image, project): the row this project
    already held is re-anchored, not duplicated. Only the fields populate
    names are written, so `needs_reprocess` survives (v1 lost 259 flags in an
    hour to a writer that wrote every column)."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    old = await _species(owner_engine, lab, capture, project=70, task=11,
                         superseded=True, needs_reprocess=True, grouping="x")  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        await record_species_label(
            conn, lab, capture_id=capture, ls_project_id=70, ls_task_id=12,
            image_url="s3://b/k.JPG",
        )  # fmt: skip

    row = await _row(owner_engine, "species_labels", old)
    assert (row.ls_task_id, row.superseded, row.completed) == (12, False, False)
    assert row.needs_reprocess is True
    assert row.grouping is None


async def test_superseding_retires_only_the_named_open_rows(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b = [await _capture(app_engine, lab, dive, n) for n in "ab"]
    stale = await _species(owner_engine, lab, a, project=99)
    finished = await _species(owner_engine, lab, b, project=99, completed=True)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await supersede_species_labels(conn, lab, [stale, finished]) == 1

    assert (await _row(owner_engine, "species_labels", stale)).superseded
    assert not (await _row(owner_engine, "species_labels", finished)).superseded


# -- stage 6.1 -------------------------------------------------------------------------


async def test_grouping_facts(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b = [
        await _capture(app_engine, lab, dive, n, at=T0 + timedelta(seconds=i))
        for i, n in enumerate("ab")
    ]
    await _cluster(owner_engine, lab, dive, "prediction", [b, a])
    live = await _species(
        owner_engine, lab, a, project=70, grouping="Part of previous group"
    )
    await _species(owner_engine, lab, b, project=70, superseded=True)

    async with tenant_transaction(app_engine, lab) as conn:
        facts = await species_grouping_facts(conn, lab, dive)

    assert facts.already_grouped is False
    assert facts.prediction_clusters == [[a, b]]
    assert [(r.id, r.grouping) for r in facts.species_labels] == [
        (live, "Part of previous group")
    ]


async def test_label_studio_clusters_are_written_all_or_nothing(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    a, b, c = [await _capture(app_engine, lab, dive, n) for n in "abc"]

    async with tenant_transaction(app_engine, lab) as conn:
        assert await persist_label_studio_clusters(conn, lab, dive, [[a, b], [c]]) == 2

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT k.formed_by, k.updated_at, array_agg(m.capture_id) AS m "
                    "FROM dive_frame_clusters k JOIN dive_frame_cluster_captures m "
                    "ON m.cluster_id = k.id WHERE k.dive_id = :d GROUP BY k.id"
                ),
                {"d": dive},
            )
        ).all()
    assert sorted(len(r.m) for r in rows) == [1, 2]
    assert {r.formed_by for r in rows} == {"label_studio"}
    assert all(r.updated_at is not None for r in rows)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await persist_label_studio_clusters(conn, lab, dive, [[a]]) is None
        facts = await species_grouping_facts(conn, lab, dive)
    assert facts.already_grouped is True


async def test_a_foreign_capture_writes_nothing(owner_engine, app_engine):
    """v1 posted cluster by cluster, so a bad one left the ones before it --
    and v1 refuses to re-run while any exist. Nothing is written here."""
    lab = await _tenant(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    other = await _dive(app_engine, lab, "d2")
    mine = await _capture(app_engine, lab, dive, "a")
    theirs = await _capture(app_engine, lab, other, "a")

    with pytest.raises(InvalidClusters):
        async with tenant_transaction(app_engine, lab) as conn:
            await persist_label_studio_clusters(conn, lab, dive, [[mine], [theirs]])

    async with tenant_transaction(app_engine, lab) as conn:
        assert (await species_grouping_facts(conn, lab, dive)).already_grouped is False


# -- the dive links the species sync writes -------------------------------------------


async def _reference(owner_engine):
    """A slate and two versions of a board. Reference data is global and
    outlives each test's tenants, so the names are the test's own."""
    suffix = uuid.uuid4().hex[:8]
    async with owner_engine.begin() as conn:
        slate = (
            await conn.execute(
                text(
                    "INSERT INTO slate_templates (name, reference_points) "
                    "VALUES (:n, '[]') RETURNING id"
                ),
                {"n": f"V-Slate {suffix}"},
            )
        ).scalar_one()
        new, old = [
            (
                await conn.execute(
                    text(
                        "INSERT INTO calibration_targets (name, interior_rows, "
                        "interior_cols, pitch_x_m, pitch_y_m, valid_from) VALUES "
                        "(:n, 10, 14, :p, :p, :v) RETURNING id"
                    ),
                    {"n": f"Board {suffix}", "p": pitch, "v": valid_from},
                )
            ).scalar_one()
            # The correction first: whatever order rows come back in, only
            # `valid_from` may decide which is current.
            for pitch, valid_from in ((0.021, T0 + timedelta(days=1)), (0.02, T0))
        ]
    return suffix, slate, old, new


async def test_reference_names_resolve_through_current_rows(owner_engine, app_engine):
    """Calibration targets are versioned by `valid_from`, so a name resolves
    to its current row (v1's names were unique, one row each)."""
    lab = await _tenant(owner_engine)
    suffix, slate, _, current = await _reference(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        slates = await slate_templates_by_name(conn)
        targets = await calibration_targets_by_name(conn)

    assert slates[f"V-Slate {suffix}"] == slate
    assert targets[f"Board {suffix}"] == current


async def _refuse(owner_engine, tenant, dive):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, "
                "outcome, refusal_reason) VALUES (:t, :d, 'slate', 'refused', 'x')"
            ),
            {"t": tenant, "d": dive},
        )


async def _outlived(app_engine, tenant, dive):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await refusal_outlived_by_link_change(conn, tenant, dive)


async def test_writing_a_link_sets_it_and_expires_a_standing_refusal(
    owner_engine, app_engine
):
    """v1's set_dive_slate / set_calibration_target cleared the refusal
    columns. v2's refusal is an append-only row, so the write stamps the dive
    and a refusal older than the stamp has expired."""
    lab = await _tenant(owner_engine)
    _, slate, _, target = await _reference(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    await _refuse(owner_engine, lab, dive)
    assert await _outlived(app_engine, lab, dive) is False

    async with tenant_transaction(app_engine, lab) as conn:
        assert await set_dive_slate_template(conn, lab, dive, slate)
    row = await _row(owner_engine, "dives", dive)
    assert row.slate_template_id == slate
    assert row.calibration_links_changed_at is not None
    assert await _outlived(app_engine, lab, dive) is True

    # A refusal after the link change stands again...
    await _refuse(owner_engine, lab, dive)
    assert await _outlived(app_engine, lab, dive) is False
    # ...until the next link write, the calibration target's included.
    async with tenant_transaction(app_engine, lab) as conn:
        assert await set_dive_calibration_target(conn, lab, dive, target)
    assert (await _row(owner_engine, "dives", dive)).calibration_target_id == target
    assert await _outlived(app_engine, lab, dive) is True


async def test_a_dive_with_no_refusal_has_nothing_to_outlive(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    _, slate, _, _ = await _reference(owner_engine)
    dive = await _dive(app_engine, lab, "d1")
    async with tenant_transaction(app_engine, lab) as conn:
        await set_dive_slate_template(conn, lab, dive, slate)

    assert await _outlived(app_engine, lab, dive) is False


async def test_the_unidentified_slate_note_never_clobbers_an_operators(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    empty = await _dive(app_engine, lab, "d1")
    noted = await _dive(app_engine, lab, "d2")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE dives SET notes = 'operator: re-shoot' WHERE id = :d"),
            {"d": noted},
        )

    async with tenant_transaction(app_engine, lab) as conn:
        assert await note_unidentified_slate(conn, lab, empty, "not in list")
        assert not await note_unidentified_slate(conn, lab, noted, "not in list")

    assert (await _row(owner_engine, "dives", empty)).notes == "not in list"
    assert (await _row(owner_engine, "dives", noted)).notes == "operator: re-shoot"
    # Only notes: never priority (v1's rule -- parking stays a human decision).
    assert (await _row(owner_engine, "dives", empty)).priority == "high"


# -- as the orchestrator's service principal ------------------------------------------


async def test_the_catalog_acts_only_in_tenants_it_serves(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    lab = tenants["lab"]
    reef = await _tenant(owner_engine, "reef")
    dive = await _dive(app_engine, lab, "d1")
    capture = await _capture(app_engine, lab, dive, "a")
    await _cluster(owner_engine, lab, dive, "prediction", [capture])
    await _laser(owner_engine, lab, capture)
    catalog = SpeciesCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert (await catalog.next_dive_for_species_preprocessing(lab)).dive_id == dive
    assert [c.dive_id for c in await catalog.dives_needing_species_population(lab)] == [
        dive
    ]
    with pytest.raises(PermissionError):
        await catalog.next_dive_for_species_preprocessing(reef)
