"""The database side of the laser-depth stage, tenant-scoped.

The cohort tests are ported from fishsense-lite@77e8f8e5
services/fishsense-api/tests/test_laser_depth_endpoint.py (the cohort half);
the work and persistence tests from the "which images, which skips" half of
services/fishsense-data-processing-workflow-worker/tests/
test_compute_laser_depths_activity.py, whose decisions are the store's in v2.
Names, shapes and reasons are v1's. The cohort is v1's:

* the dive is high priority and resolves to a calibration;
* some *canonical* capture carries a valid laser label (completed, not
  superseded, x and y set) and has no *current* depth: one naming a
  still-valid label **of the same capture** under the calibration the dive
  resolves to today. Keyed on the capture, not the label (dive 279).

v1's cohorts ran on SQLite, so v1 pinned its SQL's correlation by compiling it
(`test_recorded_label_check_is_correlated`, `..._subquery_is_correlated`).
v2's run on real Postgres with multi-valued seeds instead, which is what the
correlation bug needed to show (it took both selectors down in prod on
2026-08-20).

v2 changes, each pinned here:

* **the calibration is 0018's** (v1's read rule): the dive's own accepted,
  plausible calibration, else its link's; an implausible baseline is none.
  v1's cohort SQL applied no plausibility test while its activity did, so a
  dive with only an implausible fit was selected and failed for an hour;
* **the camera matrix is the current calibration of the dive's device**, and
  the cohort requires one (pinhole: the only projection this kernel has). v1
  raised in the activity when intrinsics were missing, and the dive stayed at
  the head of the cohort;
* **tried, made no progress** (PLAN.md §9.16): a capture whose every valid
  label was refused under the effective calibration drops out -- until the
  calibration changes, the dot moves, or a new label arrives. v1 counted it
  and wrote nothing (dive 32 blocked 49 dives for 23 hours);
* **appended, never overwritten**: a recompute is a new row, and a persist
  retried after a lost acknowledgement writes nothing twice;
* the processor's output is checked against the work it was given (PLAN.md
  §9.11): nothing is written for a capture or label that is not (or no longer)
  work of this dive, under the calibration it would be written with.
"""

import uuid

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    IMPLAUSIBLE_POSITION,
    T0,
    forget_identities,
    K,
    calibrate,
    calibrated_dive,
    capture,
    depth,
    device,
    dive,
    exec_,
    laser_label,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_depth_store import (
    DepthRecord,
    DepthRefusal,
    LaserDepthCatalog,
    dive_geometry,
    laser_depth_work,
    next_dive_for_laser_depth,
    persist_laser_depths,
)
from fishsense_services_api.service_principal import NotAMember

ORCHESTRATOR = "service:fishsense-orchestrator"


async def _next(app_engine, tenant_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        candidate = await next_dive_for_laser_depth(conn, tenant_id)
    return None if candidate is None else candidate.dive_id


async def _work(app_engine, tenant_id, dive_id):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await laser_depth_work(conn, tenant_id, dive_id)


async def _persist(app_engine, tenant_id, dive_id, calibration, depths=(), refusals=()):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await persist_laser_depths(
            conn,
            tenant_id,
            dive_id,
            laser_calibration_id=calibration,
            core_version="4.1.0",
            depths=list(depths),
            refusals=list(refusals),
        )


async def _labelled(owner_engine, tenant_id, dive_id, **label):
    capture_id = await capture(owner_engine, tenant_id, dive_id)
    return capture_id, await laser_label(owner_engine, tenant_id, capture_id, **label)


# -- the cohort (v1's tests) -----------------------------------------------------


async def test_selector_picks_a_dive_whose_laser_images_have_no_depth(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_selector_requires_calibration(owner_engine, app_engine):
    """No calibration, no depth: `compute_world_point_from_laser` needs the
    laser's position and axis. Such a dive is stage 13's problem, not this
    stage's, and must not sit in the cohort forever."""
    lab = await tenant(owner_engine)
    dive_id = await dive(owner_engine, lab, device_id=await device(owner_engine, lab))
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) is None


async def test_selector_accepts_borrowed_calibration(owner_engine, app_engine):
    """A fish-only dive borrows a sibling slate dive's rig calibration via
    its calibration source."""
    lab = await tenant(owner_engine)
    slate_dive, _ = await calibrated_dive(owner_engine, lab)
    fish_dive = await dive(
        owner_engine,
        lab,
        device_id=await device(owner_engine, lab),
        source_dive=slate_dive,
    )
    await _labelled(owner_engine, lab, fish_dive)

    assert await _next(app_engine, lab) == fish_dive


async def test_selector_skips_a_dive_whose_depths_are_current(owner_engine, app_engine):
    """Drains. The depth row names the label and the calibration it came
    from; when both still match, there is nothing to recompute."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, capture_id, label, calibration)

    assert await _next(app_engine, lab) is None


async def test_selector_repicks_a_dive_after_recalibration(owner_engine, app_engine):
    """The 2026-08-11 slate panel-offset fix recalibrated dives whose depths
    had already been computed. A depth carrying the replaced calibration is
    stale, and staleness is the whole reason the provenance columns exist.
    (v2: a refit is an appended row with a new id, so this also catches the
    in-place refit v1's kept id could not.)"""
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, capture_id, label, old)
    await calibrate(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_selector_repicks_a_dive_after_the_laser_label_changes(
    owner_engine, app_engine
):
    """A superseded-then-replaced laser label moves the dot, which moves the
    depth. Keyed on the label id, so the replacement re-enters the cohort."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, old = await _labelled(owner_engine, lab, dive_id, superseded=True)
    await laser_label(owner_engine, lab, capture_id)
    await depth(owner_engine, lab, capture_id, old, calibration)

    assert await _next(app_engine, lab) == dive_id


@pytest.mark.parametrize(
    "label",
    [{"completed": False}, {"superseded": True}, {"x": None}, {"y": None}],
    ids=["incomplete", "superseded", "no-x", "no-y"],
)
async def test_selector_ignores_images_without_a_valid_laser(
    owner_engine, app_engine, label
):
    """Incomplete, superseded, or coordinate-less labels are not a laser
    fix — the same valid-laser gate stages 1/2/5.1/14 use."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await _labelled(owner_engine, lab, dive_id, **label)

    assert await _next(app_engine, lab) is None


async def test_selector_ignores_non_canonical_images(owner_engine, app_engine):
    """The prod dive 60 wedge: a duplicate dive's frames never drain because
    the pipeline declines to work on them. Every selector filters canonical."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    capture_id = await capture(owner_engine, lab, dive_id, canonical=False)
    await laser_label(owner_engine, lab, capture_id)

    assert await _next(app_engine, lab) is None


async def test_selector_skips_low_priority_dives(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab, priority="low")
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) is None


# ── duplicate valid labels (v1) ──────────────────────────────────────────


async def test_selector_drains_an_image_with_two_valid_laser_labels(
    owner_engine, app_engine
):
    """Both labels are valid and the depth names one of them — that is a
    complete answer for the image, so the dive must drop out (dive 279)."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, first = await _labelled(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, capture_id)  # duplicate of the same dot
    await depth(owner_engine, lab, capture_id, first, calibration)

    assert await _next(app_engine, lab) is None


async def test_selector_still_repicks_when_the_recorded_label_went_superseded(
    owner_engine, app_engine
):
    """The self-healing property must survive the fix: a depth whose label is
    no longer valid is stale, even though the image has another valid one."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, recorded = await _labelled(owner_engine, lab, dive_id)
    await laser_label(owner_engine, lab, capture_id)
    await depth(owner_engine, lab, capture_id, recorded, calibration)
    await exec_(
        owner_engine,
        "UPDATE laser_labels SET superseded = true WHERE id = :l",
        l=recorded,
    )

    assert await _next(app_engine, lab) == dive_id


async def test_selector_repicks_when_the_depth_names_another_images_label(
    owner_engine, app_engine
):
    """Provenance has to mean *this* image's label. With the check
    uncorrelated, any valid label anywhere satisfied it, so a depth row
    pointing at a different image's label read as current and the image was
    never recomputed."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    first, _ = await _labelled(owner_engine, lab, dive_id)
    second, second_label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, second, second_label, calibration)
    # The first image's depth names the second image's label.
    await depth(owner_engine, lab, first, second_label, calibration)

    assert await _next(app_engine, lab) == dive_id


async def test_cohort_resolves_each_dives_own_calibration_not_the_first_row(
    owner_engine, app_engine
):
    """v1's multi-valued guard (test_measurement_calibration_provenance): two
    calibrated dives, the one under test not the first. An uncorrelated
    resolution would give every dive the first row's calibration and make
    the second dive's correctly stamped depth look stale forever."""
    lab = await tenant(owner_engine)
    await calibrated_dive(owner_engine, lab)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, capture_id, label, calibration)

    assert await _next(app_engine, lab) is None


# -- the calibration it resolves to (0018, v1's rule) ----------------------------


async def test_an_implausible_calibration_counts_as_none(owner_engine, app_engine):
    """v1's `_plausible_extrinsics`: eight stored fits of 2.35-22.22 cm backed
    663 of 3,104 measurements at -75% to +45%. v1's cohort did not apply the
    test and its activity did, so such a dive was selected and failed for an
    hour at the head of the queue; here neither offers it."""
    lab = await tenant(owner_engine)
    dive_id = await dive(owner_engine, lab, device_id=await device(owner_engine, lab))
    await calibrate(owner_engine, lab, dive_id, position=IMPLAUSIBLE_POSITION)
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) is None


async def test_an_implausible_own_calibration_falls_through_to_the_link(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    source, borrowed = await calibrated_dive(owner_engine, lab)
    dive_id = await dive(
        owner_engine, lab, device_id=await device(owner_engine, lab), source_dive=source
    )
    await calibrate(owner_engine, lab, dive_id, position=IMPLAUSIBLE_POSITION)
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id
    assert (await _work(app_engine, lab, dive_id)).geometry.laser_calibration_id == (
        borrowed
    )


async def test_the_dives_own_calibration_wins_over_its_link(owner_engine, app_engine):
    """v1's `get_laser_extrinsics_for_dive`: own first, then the link."""
    lab = await tenant(owner_engine)
    source, _ = await calibrated_dive(owner_engine, lab)
    dive_id = await dive(
        owner_engine, lab, device_id=await device(owner_engine, lab), source_dive=source
    )
    own = await calibrate(owner_engine, lab, dive_id)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, capture_id, label, own)

    assert await _next(app_engine, lab) is None


# -- v2: the camera matrix -------------------------------------------------------


async def test_the_camera_matrix_is_the_current_calibration_of_the_dives_device(
    owner_engine, app_engine
):
    """v1 read the one `cameraintrinsics` row of `dive.camera_id`. Camera
    calibrations are append-only per device in v2; the latest is current."""
    lab = await tenant(owner_engine)
    device_id = await device(owner_engine, lab)
    newer = [[2900.0, 0.0, 2000.0], [0.0, 2900.0, 1500.0], [0.0, 0.0, 1.0]]
    await exec_(
        owner_engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_matrix, "
        "distortion_coefficients) VALUES (:t, :d, CAST(:k AS jsonb), '[]')",
        t=lab,
        d=device_id,
        k=str(newer),
    )
    dive_id = await dive(owner_engine, lab, device_id=device_id)
    calibration = await calibrate(owner_engine, lab, dive_id)

    async with tenant_transaction(app_engine, lab) as conn:
        geometry = await dive_geometry(conn, lab, dive_id)

    assert geometry.camera_matrix == tuple(tuple(row) for row in newer)
    assert geometry.laser_calibration_id == calibration
    assert geometry.laser_position == (0.1, 0.0, 0.0)
    assert geometry.laser_axis == (0.0, 0.0, 1.0)


@pytest.mark.parametrize(
    "camera",
    ["no device", "no calibration", "axial refractive", "singular matrix"],
)
async def test_a_dive_the_kernel_cannot_project_is_not_offered(
    owner_engine, app_engine, camera
):
    """v2. v1 raised `ValueError` in the activity when the dive's camera had
    no intrinsics, retried it for an hour, and left the dive at the head of
    its cohort. A dive with no usable pinhole calibration is not work for
    this kernel -- it projects through K^-1 and nothing else."""
    lab = await tenant(owner_engine)
    if camera == "no device":
        device_id = None
    elif camera == "no calibration":
        device_id = await device(owner_engine, lab, camera_matrix=False)
    elif camera == "axial refractive":
        device_id = await device(owner_engine, lab, model="axial_refractive")
    else:
        device_id = await device(
            owner_engine, lab, camera_matrix=[[1.0, 0, 0], [0, 0, 0], [0, 0, 1.0]]
        )
    dive_id = await dive(owner_engine, lab, device_id=device_id)
    await calibrate(owner_engine, lab, dive_id)
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) is None


async def test_a_directionless_laser_axis_is_not_offered(owner_engine, app_engine):
    """v2. fishsense-core refuses a zero axis, for every image of the dive
    alike; v1 selected the dive and failed it every hour."""
    lab = await tenant(owner_engine)
    dive_id = await dive(owner_engine, lab, device_id=await device(owner_engine, lab))
    await calibrate(owner_engine, lab, dive_id, axis=(0.0, 0.0, 0.0))
    await _labelled(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) is None


# -- v2: tried, made no progress (PLAN.md §9.16) ---------------------------------


async def _refuse(app_engine, lab, dive_id, calibration, capture_id, label, x, y):
    return await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        refusals=[
            DepthRefusal(
                capture_id=capture_id,
                laser_label_id=label,
                x=x,
                y=y,
                reason="non_positive_depth",
                depth_m=-0.4,
            )
        ],
    )


async def test_a_capture_whose_every_label_was_refused_drops_out(
    owner_engine, app_engine
):
    """The dive-32 wedge: every valid label triangulated behind the camera,
    v1 wrote nothing, and the dive blocked 49 others for 23 hours."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)

    await _refuse(
        app_engine, lab, dive_id, calibration, capture_id, label, 30.0, 3000.0
    )

    assert await _next(app_engine, lab) is None
    work = await _work(app_engine, lab, dive_id)
    assert work.captures == []
    assert work.skipped_refused == 1


async def test_a_refusal_expires_when_the_calibration_changes(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)
    await _refuse(
        app_engine, lab, dive_id, calibration, capture_id, label, 30.0, 3000.0
    )

    await calibrate(owner_engine, lab, dive_id)

    assert await _next(app_engine, lab) == dive_id


async def test_a_refusal_expires_when_the_dot_moves(owner_engine, app_engine):
    """Label Studio sync edits a label in place: the same label with a new
    dot is a new input, and is tried again."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)
    await _refuse(
        app_engine, lab, dive_id, calibration, capture_id, label, 30.0, 3000.0
    )

    await exec_(owner_engine, "UPDATE laser_labels SET x = 1900 WHERE id = :l", l=label)

    assert await _next(app_engine, lab) == dive_id


async def test_a_new_valid_label_is_tried_after_a_refusal(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)
    await _refuse(
        app_engine, lab, dive_id, calibration, capture_id, label, 30.0, 3000.0
    )
    fresh = await laser_label(owner_engine, lab, capture_id, x=1900.0, y=1400.0)

    work = await _work(app_engine, lab, dive_id)

    assert [
        (c.capture_id, [d.laser_label_id for d in c.laser_labels])
        for c in work.captures
    ] == [(capture_id, [fresh])]


# -- the work handed to the processor (v1's activity tests) ----------------------


async def test_work_lists_each_captures_valid_labels_in_label_order(
    owner_engine, app_engine
):
    """v1 `_valid_labels_by_image`: grouped by image, each list in ascending
    id order (v2: the label's number, v1's id for a migrated label), so the
    label an image settles on is deterministic across re-runs."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    first = await capture(owner_engine, lab, dive_id)
    a = await laser_label(owner_engine, lab, first, x=1.0, y=2.0)
    b = await laser_label(owner_engine, lab, first, x=3.0, y=4.0)
    second, c = await _labelled(owner_engine, lab, dive_id, x=5.0, y=6.0)

    work = await _work(app_engine, lab, dive_id)

    assert work.geometry.laser_calibration_id == calibration
    assert work.geometry.camera_matrix == tuple(tuple(row) for row in K)
    assert [
        (c_.capture_id, [(d.laser_label_id, d.x, d.y) for d in c_.laser_labels])
        for c_ in work.captures
    ] == [(first, [(a, 1.0, 2.0), (b, 3.0, 4.0)]), (second, [(c, 5.0, 6.0)])]


async def test_work_skips_captures_already_current_and_counts_them(
    owner_engine, app_engine
):
    """v1's `skipped_current`: the same label and the same calibration produce
    the same number, so recomputing it is pure cost."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    done, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, done, label, calibration)
    todo, _ = await _labelled(owner_engine, lab, dive_id)

    work = await _work(app_engine, lab, dive_id)

    assert [c.capture_id for c in work.captures] == [todo]
    assert work.skipped_current == 1


async def test_work_counts_labels_that_are_not_a_validated_fix(
    owner_engine, app_engine
):
    """v1's `skipped_unusable_label`, per label: a dot a labeler placed but
    nobody validated is not a position to derive a distance from."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await _labelled(owner_engine, lab, dive_id, completed=False)
    await _labelled(owner_engine, lab, dive_id, x=None, y=None)

    work = await _work(app_engine, lab, dive_id)

    assert work.captures == []
    assert work.skipped_unusable_label == 2


async def test_work_is_canonical_only(owner_engine, app_engine):
    """The cohort is canonical-only, so the work must be: work dispatched that
    the cohort did not promise is how a dive never drains (v1's activity
    walked every image's labels)."""
    lab = await tenant(owner_engine)
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    copy = await capture(owner_engine, lab, dive_id, canonical=False)
    await laser_label(owner_engine, lab, copy)

    assert (await _work(app_engine, lab, dive_id)).captures == []


async def test_work_for_an_uncalibrated_dive_has_no_geometry(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    dive_id = await dive(owner_engine, lab)

    work = await _work(app_engine, lab, dive_id)

    assert work.geometry is None
    assert work.captures == []


# -- persisting --------------------------------------------------------------------


async def _depths(owner_engine, capture_id):
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT laser_label_id, laser_calibration_id, depth_m, range_m, "
                "residual_m, core_version FROM laser_depths WHERE capture_id = :c "
                "ORDER BY seq"
            ),
            {"c": capture_id},
        )
        return [tuple(r) for r in rows]


async def test_persist_records_depth_range_residual_and_provenance(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        depths=[DepthRecord(capture_id, label, 100.0, 200.0, 1.2, 1.25, 3e-6)],
    )

    assert persisted.written == 1
    assert await _depths(owner_engine, capture_id) == [
        (label, calibration, 1.2, 1.25, 3e-6, "4.1.0")
    ]
    assert await _next(app_engine, lab) is None


async def test_a_recompute_is_appended_not_overwritten(owner_engine, app_engine):
    """v1 upserted on image_id; v2 keeps the old row as history and reads the
    latest as current."""
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await depth(owner_engine, lab, capture_id, label, old, depth_m=9.99)
    new = await calibrate(owner_engine, lab, dive_id)

    await _persist(
        app_engine,
        lab,
        dive_id,
        new,
        depths=[DepthRecord(capture_id, label, 100.0, 200.0, 1.2, 1.2, 0.0)],
    )

    assert [row[1:3] for row in await _depths(owner_engine, capture_id)] == [
        (old, 9.99),
        (new, 1.2),
    ]
    assert await _next(app_engine, lab) is None


async def test_persist_records_a_fallback_labels_refusal_and_its_siblings_depth(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, bad = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)
    good = await laser_label(owner_engine, lab, capture_id, x=1900.0, y=1400.0)

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        depths=[DepthRecord(capture_id, good, 1900.0, 1400.0, 1.2, 1.25, 0.0)],
        refusals=[
            DepthRefusal(capture_id, bad, 30.0, 3000.0, "non_positive_depth", -0.4)
        ],
    )

    assert (persisted.written, persisted.refused) == (1, 1)
    assert [row[0] for row in await _depths(owner_engine, capture_id)] == [good]


async def test_persisting_twice_writes_once(owner_engine, app_engine):
    """A persist retried after a lost acknowledgement must not append the
    same result again: the work it answered is no longer work."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    refused_capture, refused = await _labelled(owner_engine, lab, dive_id, x=3.0, y=4.0)
    result = dict(
        depths=[DepthRecord(capture_id, label, 100.0, 200.0, 1.2, 1.25, 0.0)],
        refusals=[
            DepthRefusal(refused_capture, refused, 3.0, 4.0, "non_finite_depth", None)
        ],
    )

    await _persist(app_engine, lab, dive_id, calibration, **result)
    again = await _persist(app_engine, lab, dive_id, calibration, **result)

    assert (again.written, again.refused, again.skipped_stale) == (0, 0, 2)
    assert len(await _depths(owner_engine, capture_id)) == 1
    async with owner_engine.connect() as conn:
        refusals = (
            await conn.execute(text("SELECT count(*) FROM laser_depth_refusals"))
        ).scalar_one()
    assert refusals == 1


async def test_a_label_superseded_since_it_was_resolved_is_not_written(
    owner_engine, app_engine
):
    """The world moved on between resolving and persisting: the cohort will
    offer the capture again with its current labels."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id)
    await exec_(
        owner_engine, "UPDATE laser_labels SET superseded = true WHERE id = :l", l=label
    )

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        depths=[DepthRecord(capture_id, label, 100.0, 200.0, 1.2, 1.25, 0.0)],
    )

    assert (persisted.written, persisted.skipped_stale) == (0, 1)
    assert await _depths(owner_engine, capture_id) == []


async def test_a_dot_moved_since_it_was_resolved_is_not_written(
    owner_engine, app_engine
):
    """Label Studio sync moves a dot in place (same label id): a depth
    computed at the old pixel answers no current work. Written, it would name
    a still-valid label and so be current forever, at the wrong pixel."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=100.0, y=200.0)
    await exec_(owner_engine, "UPDATE laser_labels SET x = 1900 WHERE id = :l", l=label)

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        depths=[DepthRecord(capture_id, label, 100.0, 200.0, 1.2, 1.25, 0.0)],
    )

    assert (persisted.written, persisted.skipped_stale) == (0, 1)
    assert await _depths(owner_engine, capture_id) == []
    assert await _next(app_engine, lab) == dive_id


async def test_a_calibration_replaced_since_it_was_resolved_writes_nothing(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    dive_id, old = await calibrated_dive(owner_engine, lab)
    capture_id, label = await _labelled(owner_engine, lab, dive_id, x=30.0, y=3000.0)
    await calibrate(owner_engine, lab, dive_id)

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        old,
        refusals=[
            DepthRefusal(capture_id, label, 30.0, 3000.0, "non_positive_depth", -1.0)
        ],
    )

    assert (persisted.refused, persisted.skipped_stale) == (0, 1)
    assert await _next(app_engine, lab) == dive_id


async def test_a_capture_that_is_not_work_of_this_dive_is_not_written(
    owner_engine, app_engine
):
    """PLAN.md §9.11: the processor's output is checked against the work,
    not trusted -- here a capture of another dive, and a label of another
    capture."""
    lab = await tenant(owner_engine)
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    other_dive, _ = await calibrated_dive(owner_engine, lab)
    foreign, foreign_label = await _labelled(owner_engine, lab, other_dive)
    mine, _ = await _labelled(owner_engine, lab, dive_id)

    persisted = await _persist(
        app_engine,
        lab,
        dive_id,
        calibration,
        depths=[
            DepthRecord(foreign, foreign_label, 100.0, 200.0, 1.2, 1.2, 0.0),
            DepthRecord(mine, foreign_label, 100.0, 200.0, 1.2, 1.2, 0.0),
        ],
    )

    assert (persisted.written, persisted.skipped_stale) == (0, 2)
    assert await _depths(owner_engine, foreign) == []
    assert await _depths(owner_engine, mine) == []


# -- tenancy -----------------------------------------------------------------------


async def test_the_cohort_is_per_tenant_and_oldest_first(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine, "lab"), await tenant(owner_engine, "reef")
    newer, _ = await calibrated_dive(owner_engine, lab)
    older, _ = await calibrated_dive(owner_engine, lab)
    await exec_(
        owner_engine,
        "UPDATE dives SET created_at = created_at - interval '1 day' WHERE id = :d",
        d=older,
    )
    for dive_id in (newer, older):
        await _labelled(owner_engine, lab, dive_id)
    reef_dive, _ = await calibrated_dive(owner_engine, reef)
    await _labelled(owner_engine, reef, reef_dive)

    assert await _next(app_engine, lab) == older
    assert await _next(app_engine, reef) == reef_dive


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
        await _labelled(owner_engine, lab, dive_id)

    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_laser_depth(conn, lab)

    assert (candidate.dive_id, candidate.number) == (first, 900_001)


async def test_the_catalog_acts_only_in_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    other = await tenant(owner_engine, "partner")
    lab = tenants["lab"]
    dive_id, _ = await calibrated_dive(owner_engine, lab)
    await _labelled(owner_engine, lab, dive_id)
    catalog = LaserDepthCatalog(app_engine, sub=ORCHESTRATOR)

    candidate = await catalog.next_dive_for_laser_depth(lab)
    work = await catalog.laser_depth_work(lab, dive_id)

    assert candidate.dive_id == dive_id
    assert len(work.captures) == 1
    with pytest.raises(NotAMember):
        await catalog.next_dive_for_laser_depth(other)
    with pytest.raises(NotAMember):
        await catalog.persist_laser_depths(
            other,
            uuid.uuid4(),
            laser_calibration_id=uuid.uuid4(),
            core_version="4.1.0",
            depths=[],
            refusals=[],
        )
