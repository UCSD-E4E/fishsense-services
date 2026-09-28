"""The database side of stage 14 (measure fish), tenant-scoped.

Ported from fishsense-lite@77e8f8e5:

* the cohort tests from services/fishsense-api/tests/
  test_select_next_dive_endpoints.py (the measure_fish half) and
  test_measurement_calibration_provenance.py;
* the per-image decisions from services/fishsense-data-processing-workflow-
  worker/tests/test_measure_fish_activity.py -- which images are measured,
  which skipped, which fish a length binds to, and when an old binding stops
  counting. v1's activity made those decisions; in v2 the store does, and the
  processor only computes lengths (its tests keep the geometry).

Names, fixtures and reasons are v1's. The cohort is v1's: a high-priority dive
resolving to a calibration, with a canonical capture whose top-three species
label is measurable (a `Common (Scientific)` fish, a `Fish Model, <name>` or a
measurable calibration target), with a valid laser label and a valid head/tail
label, in a Label Studio cluster when it is a real fish, and with no
measurement under the calibration the dive resolves to today.

v2 changes, each pinned here:

* **one species label per capture**: the live, non-sentinel, highest-numbered
  one. v1's cohort read superseded rows too, while its activity did not -- a
  capture the cohort offered and the activity skipped, forever;
* **measurements are append-only**, so v1's stale-binding DELETE is the
  `current_measurements` view's job: a server measurement bound to a fish the
  capture's subject no longer names is history, not current, and the capture
  is work again. The old row stays;
* **tried, made no progress** (PLAN.md §9.16): a zero or non-finite length, or
  a real-fish leaf no name can be read from, is a refusal of exactly those
  inputs; the capture drops out until one of them changes. v1 dropped them
  and re-selected the dive forever;
* **species and fish models are found or created through `ensure_species` and
  `ensure_fish_model`** (migration 0027): global reference tables
  the app role may not write, with an identity-only path in;
* the processor's lengths are checked against the work they answer (PLAN.md
  §9.11), so a retry writes nothing twice.
"""

import uuid

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    REAL_FISH,
    T0,
    forget_identities,
    calibrate,
    calibrated_dive,
    capture,
    cluster,
    device,
    dive,
    exec_,
    fish,
    head_tail_label,
    laser_label,
    measurable_capture,
    measurement,
    species_label,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_depth_store import Dot
from fishsense_services_api.measurement_store import (
    HeadTailPoints,
    LengthRecord,
    MeasurementCatalog,
    measurement_work,
    next_dive_for_measurement,
    persist_measurements,
)
from fishsense_services_api.service_principal import NotAMember

ORCHESTRATOR = "service:fishsense-orchestrator"
PROVENANCE = dict(
    algorithm="laser_depth_fronto_parallel", algorithm_version="1", core_version="4.1.0"
)


def _species_names(content):
    """What the orchestrator passes: the contracts' `parse_species_names`
    (the API package does not depend on the contracts)."""
    from fishsense_services_contracts.taxonomy import parse_species_names

    return parse_species_names(content)


async def _next(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        candidate = await next_dive_for_measurement(conn, tenant_id)
    return None if candidate is None else candidate.dive_id


async def _work(app_engine, tenant_id, dive_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await measurement_work(conn, tenant_id, dive_id)


def _length(item, length_m=0.3, refusal=None):
    return LengthRecord(
        capture_id=item.capture_id,
        species_label_id=item.species_label_id,
        laser=item.laser,
        head_tail=item.head_tail,
        length_m=None if refusal else length_m,
        depth_m=1.2,
        refusal=refusal,
    )


async def _persist(app_engine, tenant_id, dive_id, calibration, lengths):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await persist_measurements(
            conn,
            tenant_id,
            dive_id,
            laser_calibration_id=calibration,
            lengths=lengths,
            species_names=_species_names,
            **PROVENANCE,
        )


async def _measure_all(app_engine, tenant_id, dive_id, calibration, **length):
    work = await _work(app_engine, tenant_id, dive_id)
    return await _persist(
        app_engine,
        tenant_id,
        dive_id,
        calibration,
        [_length(item, **length) for item in work.captures],
    )


async def _current(owner_engine, capture_id):
    """(fish, length, calibration) of the capture's current measurements."""
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT fish_id, length_m, laser_calibration_id FROM "
                "current_measurements WHERE capture_id = :c ORDER BY seq"
            ),
            {"c": capture_id},
        )
        return [tuple(r) for r in rows]


async def _cluster_fish(owner_engine, cluster_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT fish_id FROM dive_frame_clusters WHERE id = :k"),
                {"k": cluster_id},
            )
        ).scalar_one()


async def _fish_model_of(owner_engine, fish_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT m.name FROM fish f JOIN fish_models m "
                    "ON m.id = f.fish_model_id WHERE f.id = :f"
                ),
                {"f": fish_id},
            )
        ).scalar_one_or_none()


async def _species_of(owner_engine, fish_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT s.scientific_name, s.common_name FROM fish f "
                    "JOIN species s ON s.id = f.species_id WHERE f.id = :f"
                ),
                {"f": fish_id},
            )
        ).one_or_none()


# -- the cohort (v1's tests) -----------------------------------------------------


async def test_measure_fish_requires_extrinsics_and_an_unmeasured_measurable_image(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    # dive 1: measurable image but no calibration -> excluded.
    # dive 2: calibration + measurable image already measured -> excluded.
    # dive 3: calibration + unmeasured measurable image -> picked.
    d1 = await dive(owner_engine, lab, device_id=await device(owner_engine, lab))
    d2, cal2 = await calibrated_dive(owner_engine, lab)
    d3, _ = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, d1)
    measured = await measurable_capture(owner_engine, lab, d2)
    await measurable_capture(owner_engine, lab, d3)
    await measurement(
        owner_engine, lab, measured, await fish(owner_engine, lab), cal2, v1_id=1
    )

    assert await _next(app_engine, lab) == d3


async def test_measure_fish_selects_fish_model_dive_without_cluster(
    owner_engine, app_engine
):
    """A fish-model dive with no Label Studio cluster is selected once it has
    an unmeasured model image (the cluster gate is waived for models)…"""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish Model, Grouper", in_cluster=False
    )

    assert await _next(app_engine, lab) == dive_id

    # …and it drops out once the model image is measured.
    grouper = await fish(owner_engine, lab, model="Grouper")
    await measurement(owner_engine, lab, image, grouper, calibration)
    assert await _next(app_engine, lab) is None


async def test_measure_fish_ignores_unbound_clusters_with_no_measurable_image(
    owner_engine, app_engine
):
    """The regression (prod dive 466, 1632 unbound clusters against 24
    measurable images): an unbound cluster stage 14 can never touch must not
    keep the dive in the cohort forever."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id)
    await measurement(
        owner_engine, lab, image, await fish(owner_engine, lab), calibration
    )
    await cluster(owner_engine, lab, dive_id, [], fish_id=None)

    assert await _next(app_engine, lab) is None


async def test_measure_fish_selects_dive_with_borrowed_calibration(
    owner_engine, app_engine
):
    """A fish dive with no calibration of its own is still measurable when it
    links to a slate dive that owns one."""
    lab = await tenant(owner_engine)
    slate_dive, _ = await calibrated_dive(owner_engine, lab)
    fish_dive = await dive(
        owner_engine,
        lab,
        device_id=await device(owner_engine, lab),
        source_dive=slate_dive,
    )
    await measurable_capture(owner_engine, lab, fish_dive)

    assert await _next(app_engine, lab) == fish_dive


async def test_measure_fish_link_to_uncalibrated_source_does_not_select(
    owner_engine, app_engine
):
    """A link to a source that owns no calibration doesn't fabricate one."""
    lab = await tenant(owner_engine)
    source = await dive(owner_engine, lab)
    fish_dive = await dive(
        owner_engine, lab, device_id=await device(owner_engine, lab), source_dive=source
    )
    await measurable_capture(owner_engine, lab, fish_dive)

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize(
    "content",
    [
        "Slate, Laser on slate",
        "Calibration Targets, Slate",
        "Calibration Targets, E4E Checkerboard",
        "Fish Model,",
        None,
    ],
    ids=["slate-marker", "calibration-target", "checkerboard", "empty-leaf", "empty"],
)
async def test_measure_fish_skips_species_rows_without_a_scientific_name(
    owner_engine, app_engine, content
):
    """Nothing downstream can ever measure these, so they must not hold a
    dive in the cohort (the never-goes-false shape that blocked scheduling
    stage 14 before 2026-07-17; the `Fish Model,` empty leaf)."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, dive_id, content)

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize(
    "content",
    [REAL_FISH, "Fish Model, Weasly Fish", "Calibration Targets, Ruler"],
    ids=["real-fish", "model", "ruler"],
)
async def test_measure_fish_still_selects_a_measurable_row(
    owner_engine, app_engine, content
):
    """Guard the other direction."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, dive_id, content)

    assert await _next(app_engine, lab) == dive_id


async def test_a_real_fish_outside_any_label_studio_cluster_is_not_selected(
    owner_engine, app_engine
):
    """v1's `missing_cluster`: a real fish's identity is its cluster, so
    without one it cannot be bound. A prediction cluster is not one."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, in_cluster=False)
    await cluster(owner_engine, lab, dive_id, [image], formed_by="prediction")

    assert await _next(app_engine, lab) is None
    assert (await _work(app_engine, lab, dive_id)).missing_cluster == 1


@pytest.mark.parametrize(
    "missing",
    [
        {"laser": {"completed": False}},
        {"laser": {"superseded": True}},
        {"laser": {"x": None}},
        {"head_tail": {"completed": False}},
        {"head_tail": {"superseded": True}},
        {"head_tail": {"head": (None, 2.0)}},
        {"head_tail": {"tail": (3.0, None)}},
    ],
    ids=[
        "laser-incomplete",
        "laser-superseded",
        "laser-no-x",
        "headtail-incomplete",
        "headtail-superseded",
        "no-head",
        "no-tail",
    ],
)
async def test_a_capture_without_a_valid_laser_and_headtail_is_not_selected(
    owner_engine, app_engine, missing
):
    """v1's `missing_laser_or_headtail`, and the cohort's valid-laser and
    valid-headtail gates, which the activity read differently in v1 (the
    first live row, no completed filter): here both read the same rows."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image, **missing.get("laser", {}))
    await head_tail_label(owner_engine, lab, image, **missing.get("head_tail", {}))
    await species_label(owner_engine, lab, image)
    await cluster(owner_engine, lab, dive_id, [image])

    assert await _next(app_engine, lab) is None
    assert (await _work(app_engine, lab, dive_id)).missing_laser_or_headtail == 1


async def test_filters_out_non_top_three_labels(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image)
    await head_tail_label(owner_engine, lab, image)
    await species_label(owner_engine, lab, image, top_three=False)
    await cluster(owner_engine, lab, dive_id, [image])

    assert await _next(app_engine, lab) is None


async def test_non_canonical_and_low_priority_are_not_selected(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    parked, _ = await calibrated_dive(owner_engine, lab, priority="low")
    await measurable_capture(owner_engine, lab, parked)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    copy = await capture(owner_engine, lab, dive_id, canonical=False)
    await laser_label(owner_engine, lab, copy)
    await head_tail_label(owner_engine, lab, copy)
    await species_label(owner_engine, lab, copy)
    await cluster(owner_engine, lab, dive_id, [copy])

    assert await _next(app_engine, lab) is None


# ── calibration provenance (v1's test_measurement_calibration_provenance) ──


async def test_cohort_skips_a_dive_measured_with_the_current_calibration(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine,
        lab,
        image,
        await fish(owner_engine, lab, model="Grouper"),
        calibration,
    )

    assert await _next(app_engine, lab) is None


async def test_cohort_repicks_a_dive_after_recalibration(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine, lab, image, await fish(owner_engine, lab, model="Grouper"), old
    )
    await calibrate(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_cohort_repicks_a_dive_whose_measurements_predate_provenance(
    owner_engine, app_engine
):
    """A migrated measurement that names no calibration is stale once, gets
    recomputed under the current calibration, and drains."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine,
        lab,
        image,
        await fish(owner_engine, lab, model="Grouper"),
        None,
        v1_id=7,
    )

    assert await _next(app_engine, lab) == dive_id


async def test_cohort_resolves_provenance_through_a_borrowed_calibration(
    owner_engine, app_engine
):
    """A fish-only dive is measured with the sibling's calibration, so that
    is the id its measurements must carry -- not its own (it has none)."""
    lab = await tenant(owner_engine)
    source, borrowed = await calibrated_dive(owner_engine, lab)
    dive_id = await dive(
        owner_engine, lab, device_id=await device(owner_engine, lab), source_dive=source
    )
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine,
        lab,
        image,
        await fish(owner_engine, lab, model="Grouper"),
        borrowed,
    )

    assert await _next(app_engine, lab) is None


async def test_cohort_resolves_each_dives_own_calibration_not_the_first_row(
    owner_engine, app_engine
):
    """Two calibrated dives, and the one under test is NOT the first: an
    uncorrelated resolution would make its correctly stamped measurement
    look stale forever (the 2026-08-20 outage's shape)."""
    lab = await tenant(owner_engine)
    await calibrated_dive(owner_engine, lab)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine,
        lab,
        image,
        await fish(owner_engine, lab, model="Grouper"),
        calibration,
    )

    assert await _next(app_engine, lab) is None


# -- v2: one species label per capture -------------------------------------------


async def test_a_superseded_species_label_is_not_the_subject(owner_engine, app_engine):
    """v1's cohort read superseded species rows, its activity did not: a
    capture offered and skipped forever. Neither reads them here."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image)
    await head_tail_label(owner_engine, lab, image)
    await species_label(owner_engine, lab, image, superseded=True)
    await cluster(owner_engine, lab, dive_id, [image])

    assert await _next(app_engine, lab) is None


async def test_a_sentinel_species_label_is_not_the_subject(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image)
    await head_tail_label(owner_engine, lab, image)
    await species_label(owner_engine, lab, image, sentinel=True)
    await cluster(owner_engine, lab, dive_id, [image])

    assert await _next(app_engine, lab) is None


async def test_the_highest_numbered_live_species_label_is_the_subject(
    owner_engine, app_engine
):
    """Two live labels (two projects) on one capture: v1 iterated both, and
    two model names flipped the binding on every run. One wins here."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image)
    await head_tail_label(owner_engine, lab, image)
    await species_label(owner_engine, lab, image, "Fish Model, Snook")
    newer = await species_label(owner_engine, lab, image, "Fish Model, Grouper")

    (item,) = (await _work(app_engine, lab, dive_id)).captures

    assert (item.species_label_id, item.model_name) == (newer, "Grouper")


# -- the work handed to the processor (v1's activity tests) ----------------------


async def test_work_carries_the_subject_and_the_lowest_numbered_valid_labels(
    owner_engine, app_engine
):
    """v1 read "the first non-superseded" laser and head/tail label with no
    completed filter and no order -- nondeterministic for an image with
    several. v2 reads the lowest-numbered valid one of each (the depth
    stage's preference), so the inputs are the cohort's and are stable."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await capture(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, image, completed=False, x=9.0, y=9.0)
    first_laser = await laser_label(owner_engine, lab, image, x=1.0, y=2.0)
    await laser_label(owner_engine, lab, image, x=5.0, y=6.0)
    first_ht = await head_tail_label(
        owner_engine, lab, image, head=(10, 20), tail=(30, 40)
    )
    await head_tail_label(owner_engine, lab, image)
    label = await species_label(owner_engine, lab, image)
    cluster_id = await cluster(owner_engine, lab, dive_id, [image])

    work = await _work(app_engine, lab, dive_id)

    assert work.geometry.laser_calibration_id == calibration
    (item,) = work.captures
    assert item.capture_id == image
    assert item.species_label_id == label
    assert (item.content_of_image, item.real_fish, item.model_name) == (
        REAL_FISH,
        True,
        None,
    )
    assert (item.cluster_id, item.cluster_fish_id) == (cluster_id, None)
    assert item.laser == Dot(first_laser, 1.0, 2.0)
    assert item.head_tail == HeadTailPoints(first_ht, 10.0, 20.0, 30.0, 40.0)


async def test_measures_only_the_unmeasured_images(owner_engine, app_engine):
    """v1's per-dive skip: re-measuring means re-deriving a length and
    re-binding a fish for work already done."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    done = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    todo = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(
        owner_engine,
        lab,
        done,
        await fish(owner_engine, lab, model="Grouper"),
        calibration,
    )

    work = await _work(app_engine, lab, dive_id)

    assert [item.capture_id for item in work.captures] == [todo]
    assert work.skipped_already_measured == 1


async def test_work_counts_unmeasurable_species(owner_engine, app_engine):
    """v1's `skipped_unmeasurable_species`, its own counter so it points at
    the taxonomy branch rather than at the labels."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, dive_id, "Slate, Laser on slate")

    work = await _work(app_engine, lab, dive_id)

    assert work.captures == []
    assert work.skipped_unmeasurable_species == 1
    assert work.missing_laser_or_headtail == 0, "must not inflate the label counter"


# -- persisting: fish identity (v1's activity tests) ------------------------------


async def test_measures_one_fish_end_to_end(owner_engine, app_engine):
    """Species created (none yet), fish created (the cluster had none) and the
    cluster bound to it; the measurement names its calibration, its labels
    and how it was made."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    scientific = f"Ophiodon elongatus {uuid.uuid4().hex[:6]}"
    image = await measurable_capture(
        owner_engine, lab, dive_id, f"Stuff, Lingcod ({scientific})"
    )
    (item,) = (await _work(app_engine, lab, dive_id)).captures

    persisted = await _persist(
        app_engine, lab, dive_id, calibration, [_length(item, 0.3)]
    )

    assert (persisted.measured, persisted.fish_created, persisted.clusters_bound) == (
        1,
        1,
        1,
    )
    ((fish_id, length_m, cal),) = await _current(owner_engine, image)
    assert (length_m, cal) == (0.3, calibration)
    assert await _cluster_fish(owner_engine, item.cluster_id) == fish_id
    assert await _species_of(owner_engine, fish_id) == (scientific, "Lingcod")
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT laser_label_id, head_tail_label_id, algorithm, "
                    "algorithm_version, core_version, source FROM measurements "
                    "WHERE capture_id = :c"
                ),
                {"c": image},
            )
        ).one()
    assert tuple(row) == (
        item.laser.laser_label_id,
        item.head_tail.head_tail_label_id,
        "laser_depth_fronto_parallel",
        "1",
        "4.1.0",
        "server",
    )
    assert await _next(app_engine, lab) is None


async def test_existing_species_and_fish_are_reused(owner_engine, app_engine):
    """The cluster already points at a fish: measure against it, and do not
    re-bind. (A species relabel does not change a real fish's identity.)"""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    existing = await fish(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, cluster_fish=existing)
    (item,) = (await _work(app_engine, lab, dive_id)).captures

    persisted = await _persist(app_engine, lab, dive_id, calibration, [_length(item)])

    assert (persisted.fish_created, persisted.clusters_bound) == (0, 0)
    assert [row[0] for row in await _current(owner_engine, image)] == [existing]


async def test_one_unbound_cluster_binds_one_fish_for_all_its_frames(
    owner_engine, app_engine
):
    """v1's `_ensure_fish` per image: the first creates and binds, the next
    finds the cluster bound."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    images = [await capture(owner_engine, lab, dive_id) for _ in range(2)]
    for image in images:
        await laser_label(owner_engine, lab, image)
        await head_tail_label(owner_engine, lab, image)
        await species_label(owner_engine, lab, image)
    cluster_id = await cluster(owner_engine, lab, dive_id, images)

    persisted = await _measure_all(app_engine, lab, dive_id, calibration)

    assert (persisted.measured, persisted.fish_created) == (2, 1)
    bound = await _cluster_fish(owner_engine, cluster_id)
    for image in images:
        assert [row[0] for row in await _current(owner_engine, image)] == [bound]


async def test_measures_a_fish_model_without_a_cluster(owner_engine, app_engine):
    """The key case: a model image with NO Label Studio cluster is measured,
    against a name-keyed fish (no species), and no cluster is bound."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish Model, Grouper", in_cluster=False
    )

    persisted = await _measure_all(app_engine, lab, dive_id, calibration)

    assert (persisted.measured, persisted.clusters_bound) == (1, 0)
    ((fish_id, _, _),) = await _current(owner_engine, image)
    assert await _fish_model_of(owner_engine, fish_id) == "Grouper"
    assert await _species_of(owner_engine, fish_id) is None


async def test_a_model_no_one_registered_is_measured_and_registered(
    owner_engine, app_engine
):
    """v1 measured any `Fish Model, <name>` leaf. The model is registered as
    an identity (ensure_fish_model); its reference length, if it ever has
    one, is added the versioned way."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    name = f"Test Model {uuid.uuid4().hex[:6]}"
    image = await measurable_capture(
        owner_engine, lab, dive_id, f"Fish Model, {name}", in_cluster=False
    )

    await _measure_all(app_engine, lab, dive_id, calibration)

    ((fish_id, _, _),) = await _current(owner_engine, image)
    assert await _fish_model_of(owner_engine, fish_id) == name


async def test_reuses_existing_model_fish_by_name(owner_engine, app_engine):
    """Same model across dives resolves to ONE fish per tenant."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    existing = await fish(owner_engine, lab, model="Grouper")
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")

    persisted = await _measure_all(app_engine, lab, dive_id, calibration)

    assert persisted.clusters_bound == 0
    assert [row[0] for row in await _current(owner_engine, image)] == [existing]


async def test_two_models_in_one_cluster_resolve_to_distinct_fish(
    owner_engine, app_engine
):
    """The mixed-group case: two different models sharing a cluster must NOT
    collapse to one fish, and the cluster is never bound for them."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    images = []
    for model in ("Grouper", "Shark"):
        image = await capture(owner_engine, lab, dive_id)
        await laser_label(owner_engine, lab, image)
        await head_tail_label(owner_engine, lab, image)
        await species_label(owner_engine, lab, image, f"Fish Model, {model}")
        images.append(image)
    shared = await cluster(owner_engine, lab, dive_id, images)

    await _measure_all(app_engine, lab, dive_id, calibration)

    names = [
        await _fish_model_of(owner_engine, (await _current(owner_engine, i))[0][0])
        for i in images
    ]
    assert names == ["Grouper", "Shark"]
    assert await _cluster_fish(owner_engine, shared) is None


async def test_same_model_across_clusters_resolves_to_one_fish(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    images = [
        await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
        for _ in range(2)
    ]

    persisted = await _measure_all(app_engine, lab, dive_id, calibration)

    assert persisted.measured == 2
    fish_ids = {(await _current(owner_engine, i))[0][0] for i in images}
    assert len(fish_ids) == 1, "same model -> one fish"


# -- stale bindings: v1's DELETE, as the current_ view's rule ---------------------


async def test_stale_model_binding_is_invalidated_and_remeasured(
    owner_engine, app_engine
):
    """A species relabel leaves the measurement bound to the OLD model's fish
    (prod images 4375/4664/4868, fixed by hand in v1). The old row stops
    being current -- and stays, as history -- and the image is measured
    against the corrected model, counted once."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    snook = await fish(owner_engine, lab, model="Snook")
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish Model, Grouper", in_cluster=False
    )
    old = await measurement(owner_engine, lab, image, snook, calibration, length_m=0.44)

    assert await _current(owner_engine, image) == []
    assert await _next(app_engine, lab) == dive_id

    await _measure_all(app_engine, lab, dive_id, calibration, length_m=0.36)

    ((fish_id, length_m, _),) = await _current(owner_engine, image)
    assert await _fish_model_of(owner_engine, fish_id) == "Grouper"
    assert length_m == 0.36
    async with owner_engine.connect() as conn:
        kept = (
            await conn.execute(
                text("SELECT count(*) FROM measurements WHERE id = :m"), {"m": old}
            )
        ).scalar_one()
    assert kept == 1, "append-only: the old binding stays as history"


async def test_correct_model_binding_is_left_alone(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    grouper = await fish(owner_engine, lab, model="Grouper")
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish Model, Grouper", in_cluster=False
    )
    await measurement(owner_engine, lab, image, grouper, calibration)

    assert [row[0] for row in await _current(owner_engine, image)] == [grouper]
    assert await _next(app_engine, lab) is None


async def test_real_fish_bindings_are_never_invalidated_by_a_species_relabel(
    owner_engine, app_engine
):
    """A real fish's identity is its cluster, not its label: a species change
    does not invalidate the binding."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    wild = await fish(owner_engine, lab)
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish, Bar Jack (Caranx ruber)", cluster_fish=wild
    )
    await measurement(owner_engine, lab, image, wild, calibration)

    await exec_(
        owner_engine,
        "UPDATE species_labels SET content_of_image = :c WHERE capture_id = :i",
        c=REAL_FISH,
        i=image,
    )

    assert [row[0] for row in await _current(owner_engine, image)] == [wild]
    assert await _next(app_engine, lab) is None


async def test_a_reclustered_real_fish_binding_is_invalidated(owner_engine, app_engine):
    """Prod dives 341 and 383 (2026-09-14): image 101302 bound to both fish
    176 and fish 305 at the identical 193.4 mm, which inflated a published
    field-set count by two measurements and one individual. A row bound to a
    fish the cluster no longer points at is left over from a re-clustering."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    old_fish, current_fish = await fish(owner_engine, lab), await fish(
        owner_engine, lab
    )
    image = await measurable_capture(
        owner_engine, lab, dive_id, cluster_fish=current_fish
    )
    await measurement(owner_engine, lab, image, old_fish, calibration, length_m=0.1934)

    assert await _current(owner_engine, image) == []
    await _measure_all(app_engine, lab, dive_id, calibration, length_m=0.1934)

    assert [row[0] for row in await _current(owner_engine, image)] == [current_fish]


async def test_an_unbound_cluster_invalidates_nothing(owner_engine, app_engine):
    """No fish on the cluster means no expected binding to compare against,
    so the conservative move is to leave the row alone rather than guess."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    some_fish = await fish(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, cluster_fish=None)
    await measurement(owner_engine, lab, image, some_fish, calibration)

    assert [row[0] for row in await _current(owner_engine, image)] == [some_fish]
    assert await _next(app_engine, lab) is None


async def test_a_frame_no_longer_top_three_keeps_its_measurement(
    owner_engine, app_engine
):
    """v1 only revisited top-three frames, so a frame that left the top three
    kept whatever it was bound to. So does v2."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    snook = await fish(owner_engine, lab, model="Snook")
    image = await capture(owner_engine, lab, dive_id)
    await species_label(
        owner_engine, lab, image, "Fish Model, Grouper", top_three=False
    )
    await measurement(owner_engine, lab, image, snook, calibration)

    assert [row[0] for row in await _current(owner_engine, image)] == [snook]


# -- re-measuring on a new calibration (v1) --------------------------------------


async def test_remeasures_when_the_existing_row_used_another_calibration(
    owner_engine, app_engine
):
    """The recalibration case (the 2026-08-11 panel-offset fix hit 6 of 8
    measured dives). The old row is kept as history; the new one is current."""
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    grouper = await fish(owner_engine, lab, model="Grouper")
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(owner_engine, lab, image, grouper, old, length_m=0.30)
    new = await calibrate(owner_engine, lab, dive_id)

    await _measure_all(app_engine, lab, dive_id, new, length_m=0.31)

    assert await _current(owner_engine, image) == [(grouper, 0.31, new)]


async def test_a_migrated_measurement_under_the_effective_calibration_is_current(
    owner_engine, app_engine
):
    """Parity (PLAN.md §6.2): a v1 row names only its calibration -- no
    labels, no algorithm -- and still counts while that calibration is the
    dive's, so migrated dives are not re-measured wholesale."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    grouper = await fish(owner_engine, lab, model="Grouper")
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await measurement(owner_engine, lab, image, grouper, calibration, v1_id=42)

    assert [row[0] for row in await _current(owner_engine, image)] == [grouper]
    assert await _next(app_engine, lab) is None


# -- v2: tried, made no progress --------------------------------------------------


@pytest.mark.parametrize("reason", ["zero_length", "non_finite_length"])
async def test_a_refused_length_drops_the_capture_out(owner_engine, app_engine, reason):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")

    persisted = await _measure_all(
        app_engine, lab, dive_id, calibration, refusal=reason
    )

    assert (persisted.measured, persisted.refused) == (0, 1)
    assert await _next(app_engine, lab) is None
    assert (await _work(app_engine, lab, dive_id)).skipped_refused == 1


async def test_a_refusal_expires_when_the_keypoints_move(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await _measure_all(app_engine, lab, dive_id, calibration, refusal="zero_length")

    await exec_(
        owner_engine,
        "UPDATE head_tail_labels SET tail_x = 400 WHERE capture_id = :c",
        c=image,
    )

    assert await _next(app_engine, lab) == dive_id


async def test_a_refusal_expires_when_the_calibration_changes(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    await _measure_all(app_engine, lab, dive_id, calibration, refusal="zero_length")

    await calibrate(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_a_real_fish_leaf_no_name_can_be_read_from_is_refused(
    owner_engine, app_engine
):
    """`SQL_BROADER_THAN_PYTHON`: the cohort's `LIKE '%(%)'` accepts a leaf
    `parse_species_names` cannot split. v1 accepted that divergence as
    unreachable and would have wedged on it; v2 records a refusal, which a
    corrected label reopens."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(
        owner_engine, lab, dive_id, "Fish,  (Lachnolaimus maximus)"
    )

    persisted = await _measure_all(app_engine, lab, dive_id, calibration)

    assert (persisted.measured, persisted.refused, persisted.fish_created) == (0, 1, 0)
    assert await _next(app_engine, lab) is None

    await exec_(
        owner_engine,
        "UPDATE species_labels SET content_of_image = :c WHERE capture_id = :i",
        c=REAL_FISH,
        i=image,
    )
    assert await _next(app_engine, lab) == dive_id


# -- persisting: checked against the work ------------------------------------------


async def test_persisting_twice_writes_once(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    measured = await measurable_capture(owner_engine, lab, dive_id)
    await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    work = await _work(app_engine, lab, dive_id)
    lengths = [
        _length(work.captures[0]),
        _length(work.captures[1], refusal="zero_length"),
    ]

    await _persist(app_engine, lab, dive_id, calibration, lengths)
    again = await _persist(app_engine, lab, dive_id, calibration, lengths)

    assert (again.measured, again.refused, again.skipped_stale) == (0, 0, 2)
    assert len(await _current(owner_engine, measured)) == 1
    async with owner_engine.connect() as conn:
        counts = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM measurements), "
                    "(SELECT count(*) FROM measurement_refusals), "
                    "(SELECT count(*) FROM fish)"
                )
            )
        ).one()
    assert tuple(counts) == (1, 1, 1)


async def test_a_length_for_inputs_that_changed_since_is_not_written(
    owner_engine, app_engine
):
    """The dot moved (or a label was superseded) between resolving and
    persisting: the length answers inputs that are no longer the capture's."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    (item,) = (await _work(app_engine, lab, dive_id)).captures
    await exec_(
        owner_engine, "UPDATE laser_labels SET x = 7 WHERE capture_id = :c", c=image
    )

    persisted = await _persist(app_engine, lab, dive_id, calibration, [_length(item)])

    assert (persisted.measured, persisted.skipped_stale) == (0, 1)
    assert await _current(owner_engine, image) == []


async def test_a_length_under_a_replaced_calibration_is_not_written(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    (item,) = (await _work(app_engine, lab, dive_id)).captures
    await calibrate(owner_engine, lab, dive_id)

    persisted = await _persist(app_engine, lab, dive_id, old, [_length(item)])

    assert (persisted.measured, persisted.skipped_stale) == (0, 1)
    assert await _current(owner_engine, image) == []


async def test_a_capture_of_another_dive_is_not_written(owner_engine, app_engine):
    """PLAN.md §9.11: the processor's output is checked, not trusted."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    other, _ = await calibrated_dive(owner_engine, lab)
    foreign = await measurable_capture(owner_engine, lab, other, "Fish Model, Grouper")
    (item,) = (await _work(app_engine, lab, other)).captures

    persisted = await _persist(app_engine, lab, dive_id, calibration, [_length(item)])

    assert (persisted.measured, persisted.skipped_stale) == (0, 1)
    assert await _current(owner_engine, foreign) == []


# -- tenancy -----------------------------------------------------------------------


async def _tied_dives(owner_engine, tenant_id):
    """Two dives created in the same instant -- every migrated dive is, since
    v1 recorded no creation time -- numbered against their UUID order.
    Returns (lower-numbered, higher-numbered)."""
    first, _ = await calibrated_dive(owner_engine, tenant_id, created_at=T0)
    second, _ = await calibrated_dive(owner_engine, tenant_id, created_at=T0)
    low_uuid, high_uuid = sorted((first, second))
    for dive_id, number in ((high_uuid, 900_001), (low_uuid, 900_002)):
        await exec_(
            owner_engine,
            "UPDATE dives SET number = :n WHERE id = :d",
            n=number,
            d=dive_id,
        )
    return high_uuid, low_uuid


async def test_dives_created_together_drain_in_v1s_id_order(owner_engine, app_engine):
    """v1 took `ORDER BY id`; v2 takes the oldest, then the lowest number --
    v1's id for a migrated dive -- never the UUID, which is random."""
    lab = await tenant(owner_engine)
    first, second = await _tied_dives(owner_engine, lab)
    for dive_id in (first, second):
        await measurable_capture(owner_engine, lab, dive_id)

    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_measurement(conn, lab)

    assert (candidate.dive_id, candidate.number) == (first, 900_001)


async def test_the_catalog_acts_only_in_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    other = await tenant(owner_engine, "partner")
    lab = tenants["lab"]
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    image = await measurable_capture(owner_engine, lab, dive_id, "Fish Model, Grouper")
    catalog = MeasurementCatalog(app_engine, sub=ORCHESTRATOR)

    assert (await catalog.next_dive_for_measurement(lab)).dive_id == dive_id
    (item,) = (await catalog.measurement_work(lab, dive_id)).captures
    persisted = await catalog.persist_measurements(
        lab,
        dive_id,
        laser_calibration_id=calibration,
        lengths=[_length(item)],
        species_names=_species_names,
        **PROVENANCE,
    )

    assert persisted.measured == 1
    assert len(await _current(owner_engine, image)) == 1
    with pytest.raises(NotAMember):
        await catalog.next_dive_for_measurement(other)
