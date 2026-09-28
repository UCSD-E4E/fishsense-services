"""The database side of stage 13 and checkerboard laser calibration.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/tests/:
test_select_next_dive_endpoints.py (the stage-13 laser-calibration cohort),
test_checkerboard_calibration_cohort.py, test_calibration_refusal_cohort.py,
test_implausible_calibration_reentry.py and test_laser_extrinsics_upsert.py;
and services/fishsense-api-workflow-worker/tests/
test_checkerboard_calibration_resolver.py. Names and reasons are v1's.

**Observations, not labels.** Stage 13's cohort counts calibration
observations -- a completed, live slate label on a canonical frame that also
carries a live laser dot -- because that is what the fit counts. Prod dive 347
had 18 completed slate labels and one live dot; a cohort counting labels said
eligible, the fit refused, and the dive was re-selected hourly forever,
blocking 427 and 436. The checkerboard's observation is a canonical frame with
a live dot. The two cohorts partition: stage 13 keeps any dive it can fit.

v2 changes, each pinned here:

* per tenant; oldest first by `created_at`, re-entry candidates last;
* **a refusal is a row** of the append-only `laser_calibrations`, and it
  stands while it is the dive's current row and nothing has changed since.
  "Changed" is v1's -- a laser or slate label (any state, superseded
  included) newer, in Label Studio's clock, than the labels it was computed
  from -- plus two v1 got from `_clear_refusal`: the dive's slate template or
  calibration target changing (the refusal records which it used; for a
  board, which *version*, so a pitch correction expires it), and an
  operator's clear, appended as a row;
* the snapshot a refusal expires against (`inputs_as_of`) is taken when the
  inputs are resolved, not when the refusal is recorded: a label synced while
  the fit ran is newer than what the fit saw, and now expires it (v1 missed
  it);
* **v1's upsert is gone**: a refit appends, so it is visible to every
  provenance-mismatch cohort (v1 kept the row id, and an operator had to
  delete rows to make a refit count);
* "no calibration" is "no usable own calibration": the current row is not an
  accepted, plausible one (0018's `plausible_laser_baseline`, v1's
  `is_plausible_baseline`);
* the observations resolved for stage 13 take each frame's **lowest** live
  laser label (v1: `get_laser_label(image_id).first()`, no ordering);
* a board's geometry is read through `current_calibration_targets` by name,
  so a pitch correction (a new version row) reaches the next fit, and the fit
  records the version it used; its pitch is per axis.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    BAD_POSITION,
    DISTORTION,
    GOOD_POSITION,
    K,
    TEMPLATE_POINTS,
    T0,
    calibration_target,
    capture,
    device_with_camera,
    dive,
    later,
    laser_calibration,
    laser_label,
    slate_label,
    slate_template,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_calibration_store import (
    MIN_SLATE_LASER_POINTS,
    CalibrationInputsUnavailable,
    CalibrationRecord,
    LaserCalibrationCatalog,
    checkerboard_calibration_inputs,
    clear_calibration_refusal,
    next_dive_for_checkerboard_calibration,
    next_dive_for_laser_calibration,
    record_laser_calibration,
    slate_calibration_inputs,
)

ORCHESTRATOR = "service:fishsense-orchestrator"


async def _stage13(app_engine, lab):
    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_laser_calibration(conn, lab)
    return None if candidate is None else candidate.dive_id


async def _board(app_engine, lab):
    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_checkerboard_calibration(conn, lab)
    return None if candidate is None else candidate.dive_id


async def _observation(owner_engine, lab, dive_id, *, dot=True, completed=True,
                       canonical=True, **dot_kwargs):  # fmt: skip
    """A slate frame; with `dot`, one calibration observation."""
    frame = await capture(owner_engine, lab, dive_id, canonical=canonical)
    await slate_label(owner_engine, lab, frame, completed=completed)
    if dot:
        await laser_label(owner_engine, lab, frame, **dot_kwargs)
    return frame


async def _slate_dive(owner_engine, lab, observations=2, **kwargs):
    only = await dive(
        owner_engine, lab, slate=await slate_template(owner_engine), **kwargs
    )
    for _ in range(observations):
        await _observation(owner_engine, lab, only)
    return only


async def _board_dive(owner_engine, lab, dots=2, target=None, **kwargs):
    only = await dive(
        owner_engine,
        lab,
        target=target or await calibration_target(owner_engine),
        **kwargs,
    )
    for _ in range(dots):
        await laser_label(owner_engine, lab, await capture(owner_engine, lab, only))
    return only


# ---------- the shared thresholds ----------


def test_the_observation_floor_is_the_fits():
    """One threshold spelled on both sides of the worker boundary: a dive that
    clears the cohort's copy but not the fit's is re-selected hourly with
    nothing written (dive 347)."""
    from fishsense_services_contracts.calibration_bounds import MIN_LASER_POINTS

    assert MIN_SLATE_LASER_POINTS == MIN_LASER_POINTS


async def test_the_sql_plausibility_is_the_shared_bounds(owner_engine):
    """The API and the processor must read the identical numbers. If they
    drift, the API hands a dive back for recalibration that the processor
    then accepts unchanged -- hourly, forever."""
    from fishsense_services_contracts.calibration_bounds import (
        MAX_BASELINE_M,
        MIN_BASELINE_M,
        is_plausible_baseline,
    )

    probes = [
        [MIN_BASELINE_M, 0.0], [MAX_BASELINE_M, 0.0], [MIN_BASELINE_M - 1e-6, 0.0],
        [MAX_BASELINE_M + 1e-6, 0.0], GOOD_POSITION, BAD_POSITION, [0.0, 0.0, 0.0],
        [0.06], [],
    ]  # fmt: skip
    async with owner_engine.connect() as conn:
        for position in probes:
            in_sql = (
                await conn.execute(
                    text("SELECT plausible_laser_baseline(CAST(:p AS jsonb))"),
                    {"p": json.dumps(position)},
                )
            ).scalar_one()
            assert in_sql is is_plausible_baseline(position), position


# ---------- stage 13: the cohort ----------


async def test_laser_calibration_requires_min_completed_slate_labels(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    # dive 1: 1 observation -> below threshold, excluded.
    # dive 2: 2 observations -> picked.
    # dive 3: 3 observations but already calibrated -> excluded.
    await _slate_dive(owner_engine, lab, observations=1, created_at=T0)
    second = await _slate_dive(owner_engine, lab, observations=2, created_at=later(1))
    third = await _slate_dive(owner_engine, lab, observations=3, created_at=later(2))
    await laser_calibration(owner_engine, lab, third)

    assert await _stage13(app_engine, lab) == second


async def test_laser_calibration_needs_two_images_with_laser_dots(
    owner_engine, app_engine
):
    """The prod wedge, in miniature. A dive can have plenty of completed slate
    labels and still be uncalibratable, because a slate label without a laser
    dot on the same image contributes no observation."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=1)
    for _ in range(2):
        await _observation(owner_engine, lab, only, dot=False)

    assert await _stage13(app_engine, lab) is None


async def test_laser_calibration_selects_once_two_dots_exist(owner_engine, app_engine):
    """The converse: two observations is exactly `MIN_LASER_POINTS`."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=2)

    assert await _stage13(app_engine, lab) == only


async def test_laser_calibration_ignores_a_superseded_laser_dot(
    owner_engine, app_engine
):
    """A dead-lettered dot is invisible to the fit and must be invisible here.
    This is exactly how dive 347 got into its state."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=1)
    await _observation(owner_engine, lab, only, superseded=True)

    assert await _stage13(app_engine, lab) is None


async def test_laser_calibration_ignores_a_dotless_laser_label(
    owner_engine, app_engine
):
    """A populate-seeded placeholder carries no x/y, so it is not an
    observation."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=1)
    await _observation(owner_engine, lab, only, x=None, y=None)

    assert await _stage13(app_engine, lab) is None


async def test_laser_calibration_does_not_count_a_dot_without_a_slate_label(
    owner_engine, app_engine
):
    """An observation needs BOTH. A laser dot on a frame with no slate label
    gives the fit no plane to project onto."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=1)
    await laser_label(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _stage13(app_engine, lab) is None


async def test_laser_calibration_requires_dive_slate_id(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await dive(owner_engine, lab)
    for _ in range(2):
        await _observation(owner_engine, lab, only)

    assert await _stage13(app_engine, lab) is None


async def test_an_incomplete_or_non_canonical_slate_label_is_no_observation(
    owner_engine, app_engine
):
    """The cohort counts completed slate labels on canonical frames, v1's
    `_usable_slate_observation_count`."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, observations=1)
    await _observation(owner_engine, lab, only, completed=False)
    await _observation(owner_engine, lab, only, canonical=False)

    assert await _stage13(app_engine, lab) is None


async def test_stage_13_drains_oldest_first(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _slate_dive(owner_engine, lab, created_at=later(5))
    oldest = await _slate_dive(owner_engine, lab, created_at=later(1))

    assert await _stage13(app_engine, lab) == oldest


# ---------- the checkerboard cohort ----------


async def test_picks_a_linked_dive_with_enough_live_dots(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    unlinked = await dive(owner_engine, lab, created_at=T0)
    for _ in range(5):
        await laser_label(owner_engine, lab, await capture(owner_engine, lab, unlinked))
    linked = await _board_dive(owner_engine, lab, dots=5, created_at=later(1))

    assert await _board(app_engine, lab) == linked


async def test_requires_min_observations(owner_engine, app_engine):
    """Below the floor the fit is refused, so do not offer it."""
    lab = await tenant(owner_engine)
    await _board_dive(owner_engine, lab, dots=1, created_at=T0)
    enough = await _board_dive(owner_engine, lab, dots=2, created_at=later(1))

    assert await _board(app_engine, lab) == enough


async def test_skips_a_dive_that_already_has_its_own_calibration(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    first = await _board_dive(owner_engine, lab, dots=5, created_at=T0)
    second = await _board_dive(owner_engine, lab, dots=5, created_at=later(1))
    await laser_calibration(owner_engine, lab, first, producer="checkerboard")

    assert await _board(app_engine, lab) == second


async def test_a_borrowed_calibration_does_not_exclude_a_dive(owner_engine, app_engine):
    """Borrowing is not a substitute for calibrating yourself: a dive that can
    fit its own is exactly the dive to fit, and the effective calibration is
    own-wins-then-link, so its own answer takes over the moment it exists."""
    lab = await tenant(owner_engine)
    source = await dive(owner_engine, lab, priority="low")
    await laser_calibration(owner_engine, lab, source)
    borrower = await _board_dive(owner_engine, lab, dots=5, source=source)

    assert await _board(app_engine, lab) == borrower


async def test_ignores_dives_with_no_calibration_target(owner_engine, app_engine):
    """No link means no known scale, so there is nothing to fit against."""
    lab = await tenant(owner_engine)
    only = await dive(owner_engine, lab)
    for _ in range(9):
        await laser_label(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _board(app_engine, lab) is None


async def test_ignores_non_high_priority_dives(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _board_dive(owner_engine, lab, dots=3, priority="none")

    assert await _board(app_engine, lab) is None


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"x": None, "y": None}, "populate-seeded placeholder, no dot"),
        ({"superseded": True}, "dead-lettered by the RANSAC validator"),
    ],
)
async def test_unusable_laser_labels_do_not_count(
    owner_engine, app_engine, kwargs, why
):
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab, dots=1)
    for _ in range(2):
        await laser_label(
            owner_engine, lab, await capture(owner_engine, lab, only), **kwargs
        )

    assert await _board(app_engine, lab) is None, why


async def test_non_canonical_images_do_not_count(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab, dots=1)
    await laser_label(
        owner_engine, lab, await capture(owner_engine, lab, only, canonical=False)
    )

    assert await _board(app_engine, lab) is None


async def test_a_dive_whose_frames_hide_the_board_is_still_offered(
    owner_engine, app_engine
):
    """The known over-approximation, pinned so it stays known: SQL cannot tell
    whether the detector will find a board. Such a dive is offered, refused,
    and the refusal takes it out of the cohort (below)."""
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab, dots=4)

    assert await _board(app_engine, lab) == only


async def test_a_dive_stage_13_can_calibrate_is_left_to_stage_13(
    owner_engine, app_engine
):
    """The two calibration cohorts must be disjoint, not merely different, or
    both producers fit the same dive from different targets."""
    lab = await tenant(owner_engine)
    only = await dive(
        owner_engine,
        lab,
        slate=await slate_template(owner_engine),
        target=await calibration_target(owner_engine),
    )
    for _ in range(2):
        await _observation(owner_engine, lab, only)

    assert await _stage13(app_engine, lab) == only
    assert await _board(app_engine, lab) is None


async def test_a_slate_link_that_stage_13_cannot_use_does_not_block_the_board(
    owner_engine, app_engine
):
    """Exclusion is on stage-13 *eligibility*, not on the link existing."""
    lab = await tenant(owner_engine)
    only = await _board_dive(
        owner_engine, lab, dots=2, slate=await slate_template(owner_engine)
    )

    assert await _stage13(app_engine, lab) is None
    assert await _board(app_engine, lab) == only


async def test_a_slate_dive_below_the_threshold_falls_through_to_the_board(
    owner_engine, app_engine
):
    """One usable slate observation is not enough for stage 13."""
    lab = await tenant(owner_engine)
    only = await dive(
        owner_engine,
        lab,
        slate=await slate_template(owner_engine),
        target=await calibration_target(owner_engine),
    )
    await _observation(owner_engine, lab, only)
    await laser_label(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _stage13(app_engine, lab) is None
    assert await _board(app_engine, lab) == only


async def test_the_board_drains_oldest_first(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _board_dive(owner_engine, lab, created_at=later(5))
    oldest = await _board_dive(owner_engine, lab, created_at=later(2))
    await _board_dive(owner_engine, lab, created_at=later(9))

    assert await _board(app_engine, lab) == oldest


# ---------- implausible stored calibrations re-enter, last ----------


async def test_an_implausible_calibration_counts_as_none(owner_engine, app_engine):
    """v1's `implausible_calibration_dive_ids`: eight stored fits of 2.35 to
    22.22 cm backed 663 of 3,104 measurements at -75% to +45% error, and a
    cohort keyed on "has a row" would never refit them."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await laser_calibration(owner_engine, lab, only, position=BAD_POSITION)

    assert await _stage13(app_engine, lab) == only


async def test_re_entry_candidates_are_offered_last(owner_engine, app_engine):
    """A dive that refits to the same bad baseline and is refused again must
    not head-of-line block the healthy ones behind it (the dive-347 shape)."""
    lab = await tenant(owner_engine)
    broken = await _slate_dive(owner_engine, lab, created_at=T0)
    await laser_calibration(owner_engine, lab, broken, position=BAD_POSITION)
    fresh = await _slate_dive(owner_engine, lab, created_at=later(3))

    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_laser_calibration(conn, lab)
    assert (candidate.dive_id, candidate.reentry) == (fresh, False)

    await laser_calibration(owner_engine, lab, fresh)
    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_laser_calibration(conn, lab)
    assert (candidate.dive_id, candidate.reentry) == (broken, True)


async def test_the_board_re_enters_implausible_fits_too(owner_engine, app_engine):
    """Six of the eight bad fits came from the checkerboard producer."""
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab)
    await laser_calibration(
        owner_engine, lab, only, position=BAD_POSITION, producer="checkerboard"
    )

    assert await _board(app_engine, lab) == only


async def test_a_refit_is_appended_and_is_the_current_calibration(
    owner_engine, app_engine
):
    """v1's PUT upserted on the dive and kept the row id, so a refit in place
    was invisible to the provenance-mismatch cohorts. v2 appends: the refit is
    a new row, the old one stays as history, and the dive leaves the cohort."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    old = await laser_calibration(owner_engine, lab, only, position=BAD_POSITION)

    async with tenant_transaction(app_engine, lab) as conn:
        new = await record_laser_calibration(conn, lab, only, _accepted(GOOD_POSITION))

    async with owner_engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id FROM laser_calibrations WHERE dive_id = :d ORDER BY seq"
                    ),
                    {"d": only},
                )
            )
            .scalars()
            .all()
        )
        current = (
            await conn.execute(
                text("SELECT id FROM current_laser_calibrations WHERE dive_id = :d"),
                {"d": only},
            )
        ).scalar_one()
    assert rows == [old, new] and new != old
    assert current == new
    assert await _stage13(app_engine, lab) is None


# ---------- refusals: recorded, standing, and expiring ----------


def _accepted(position=GOOD_POSITION, **overrides) -> CalibrationRecord:
    values = {
        "producer": "slate",
        "outcome": "accepted",
        "laser_position": position,
        "laser_axis": [0.0, 0.0, 1.0],
        "refusal_reason": None,
        "gate_verdicts": {"observation_geometry": "passed"},
        "lever_arm_m": 1.2,
        "observation_count": 6,
        "core_version": "4.1.0",
        "camera_calibration_id": None,
        "slate_template_id": None,
        "calibration_target_id": None,
        "inputs_as_of": None,
    }
    values.update(overrides)
    return CalibrationRecord(**values)


def _refused(**overrides) -> CalibrationRecord:
    values = {
        "producer": "checkerboard",
        "outcome": "refused",
        "laser_position": None,
        "laser_axis": None,
        "refusal_reason": "implausible baseline",
    }
    values.update(overrides)
    return _accepted(**values)


async def _refuse(app_engine, lab, dive_id, **overrides):
    async with tenant_transaction(app_engine, lab) as conn:
        return await record_laser_calibration(conn, lab, dive_id, _refused(**overrides))


async def _refused_board_dive(owner_engine, app_engine, lab, *, label_at=T0, **kw):
    """A checkerboard dive with two dotted frames, refused at `inputs_as_of`
    (default: the labels' own timestamp, as a fresh refusal would be)."""
    target = await calibration_target(owner_engine)
    only = await dive(owner_engine, lab, target=target)
    for _ in range(2):
        await laser_label(
            owner_engine, lab, await capture(owner_engine, lab, only),
            ls_updated_at=label_at,
        )  # fmt: skip
    await _refuse(
        app_engine, lab, only, calibration_target_id=target,
        inputs_as_of=kw.get("inputs_as_of", T0),
    )  # fmt: skip
    return only, target


async def test_a_dive_with_no_refusal_is_offered(owner_engine, app_engine):
    """The control: without a refusal the cohort behaves as it always did."""
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab)

    assert await _board(app_engine, lab) == only


async def test_a_refused_dive_is_not_offered(owner_engine, app_engine):
    """The whole point -- otherwise it is re-selected hourly forever."""
    lab = await tenant(owner_engine)
    await _refused_board_dive(owner_engine, app_engine, lab)

    assert await _board(app_engine, lab) is None


async def test_relabelling_brings_it_back(owner_engine, app_engine):
    """A label newer than the refusal's inputs means the inputs changed.
    Nobody has to clear anything: fixing the labels is the signal."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(owner_engine, app_engine, lab)
    await laser_label(
        owner_engine, lab, await capture(owner_engine, lab, only),
        ls_updated_at=later(1),
    )  # fmt: skip

    assert await _board(app_engine, lab) == only


async def test_a_label_updated_before_the_refusal_does_not_bring_it_back(
    owner_engine, app_engine
):
    """Guards the comparison direction."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(owner_engine, app_engine, lab)
    await laser_label(
        owner_engine, lab, await capture(owner_engine, lab, only),
        ls_updated_at=T0,
    )  # fmt: skip

    assert await _board(app_engine, lab) is None


async def test_any_label_state_counts_as_a_change(owner_engine, app_engine):
    """v1 compares every laser and slate label on the dive -- superseded and
    incomplete included -- against the refusal: a labeler dead-lettering a bad
    dot is exactly the change that should bring the dive back."""
    lab = await tenant(owner_engine)
    for newer in (
        {"superseded": True},
        {"completed": False},
        {"x": None, "y": None},
    ):
        only, _ = await _refused_board_dive(owner_engine, app_engine, lab)
        await laser_label(
            owner_engine, lab, await capture(owner_engine, lab, only),
            ls_updated_at=later(1), **newer,
        )  # fmt: skip
        assert await _board(app_engine, lab) == only, newer
        await laser_calibration(owner_engine, lab, only, producer="checkerboard")


async def test_a_newer_slate_label_counts_too(owner_engine, app_engine):
    """Both human inputs either producer consumes: the dot, and the slate's
    reference points."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(owner_engine, app_engine, lab)
    await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only),
        ls_updated_at=later(1), superseded=True,
    )  # fmt: skip

    assert await _board(app_engine, lab) == only


async def test_a_refusal_with_no_input_snapshot_expires_on_any_label(
    owner_engine, app_engine
):
    """NULL means the dive had no labels when refused, so any label at all is
    newer -- `IS NULL OR >`, not a bare `>` that NULL would make permanent."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(
        owner_engine, app_engine, lab, inputs_as_of=None
    )

    assert await _board(app_engine, lab) == only


async def test_a_refused_dive_does_not_block_a_healthy_one(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _refused_board_dive(owner_engine, app_engine, lab)
    healthy = await _board_dive(owner_engine, lab)

    assert await _board(app_engine, lab) == healthy


async def test_a_refusal_also_holds_the_slate_cohort(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await _refuse(
        app_engine, lab, only, producer="slate", inputs_as_of=later(99),
        slate_template_id=await _slate_of(owner_engine, only),
    )  # fmt: skip

    assert await _stage13(app_engine, lab) is None


async def _slate_of(owner_engine, dive_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT slate_template_id FROM dives WHERE id = :d"),
                {"d": dive_id},
            )
        ).scalar_one()


async def test_a_new_calibration_target_expires_the_refusal(owner_engine, app_engine):
    """v1's `set_calibration_target` cleared the refusal: a different declared
    board is exactly the change a refusal should not outlive. The species
    slice writes the link; the refusal recorded which target it used."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(owner_engine, app_engine, lab)
    other = await calibration_target(owner_engine)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE dives SET calibration_target_id = :g WHERE id = :d"),
            {"g": other, "d": only},
        )

    assert await _board(app_engine, lab) == only


async def test_a_pitch_correction_expires_the_refusal(owner_engine, app_engine):
    """v2: a re-measured pitch is a new version row of the same target. v1's
    operator had to `DELETE /calibration-refused/` after correcting it; here
    the refusal names the version it read, so the correction expires it."""
    lab = await tenant(owner_engine)
    name = f"E4E {uuid.uuid4()}"
    first = await calibration_target(owner_engine, name=name, pitch_x_m=0.042)
    only = await dive(owner_engine, lab, target=first)
    for _ in range(2):
        await laser_label(owner_engine, lab, await capture(owner_engine, lab, only),
                          ls_updated_at=T0)  # fmt: skip
    await _refuse(app_engine, lab, only, calibration_target_id=first, inputs_as_of=T0)
    assert await _board(app_engine, lab) is None

    await calibration_target(owner_engine, name=name, pitch_x_m=0.04223,
                             valid_from=later(1))  # fmt: skip

    assert await _board(app_engine, lab) == only


async def test_a_new_slate_template_expires_a_slate_refusal(owner_engine, app_engine):
    """v1's `set_dive_slate` cleared the refusal."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await _refuse(
        app_engine, lab, only, producer="slate", inputs_as_of=later(99),
        slate_template_id=await _slate_of(owner_engine, only),
    )  # fmt: skip
    assert await _stage13(app_engine, lab) is None

    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE dives SET slate_template_id = :s WHERE id = :d"),
            {"s": await slate_template(owner_engine), "d": only},
        )

    assert await _stage13(app_engine, lab) == only


async def test_a_migrated_refusal_expires_only_on_labels(owner_engine, app_engine):
    """A refusal carried over from v1 recorded no target (v1's dive columns
    held none), so the link comparison cannot apply to it; it still expires
    on a newer label, and by an operator's clear."""
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab)
    await laser_calibration(
        owner_engine, lab, only, outcome="refused", producer=None,
        inputs_as_of=later(1), v1_refusal_dive_id=347,
    )  # fmt: skip

    assert await _board(app_engine, lab) is None


async def test_clearing_the_refusal_re_offers_the_dive(owner_engine, app_engine):
    """The operator override, for changes the labels do not capture -- an
    appended clear, since refusals are never edited."""
    lab = await tenant(owner_engine)
    only, _ = await _refused_board_dive(owner_engine, app_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await clear_calibration_refusal(conn, lab, only, reason="retry") is True
        assert await clear_calibration_refusal(conn, lab, only) is False, "idempotent"

    assert await _board(app_engine, lab) == only


async def test_clearing_a_dive_with_no_standing_refusal_does_nothing(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _board_dive(owner_engine, lab)
    await laser_calibration(owner_engine, lab, only, producer="checkerboard")

    async with tenant_transaction(app_engine, lab) as conn:
        assert await clear_calibration_refusal(conn, lab, only) is False


async def test_an_accepted_fit_retires_the_refusal(owner_engine, app_engine):
    """v1's PUT extrinsics cleared the refusal; appended, the accepted row is
    simply the dive's current one."""
    lab = await tenant(owner_engine)
    only, target = await _refused_board_dive(owner_engine, app_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_laser_calibration(
            conn, lab, only,
            _accepted(producer="checkerboard", calibration_target_id=target),
        )  # fmt: skip
        standing = (
            await conn.execute(
                text(
                    "SELECT outcome FROM current_laser_calibrations WHERE dive_id = :d"
                ),
                {"d": only},
            )
        ).scalar_one()
    assert standing == "accepted"


# ---------- recording ----------


async def test_a_record_carries_its_provenance(owner_engine, app_engine):
    """PLAN.md §4.3: producer, camera calibration, target, lever arm, count,
    each gate's verdict, core version, and the inputs it was computed from."""
    lab = await tenant(owner_engine)
    device, calibration = await device_with_camera(owner_engine, lab)
    target = await calibration_target(owner_engine)
    only = await dive(owner_engine, lab, device=device, target=target)
    record = _accepted(
        producer="checkerboard",
        camera_calibration_id=calibration,
        calibration_target_id=target,
        inputs_as_of=later(2),
        gate_verdicts={"observation_geometry": "passed", "describes_dive": "abstained"},
    )

    async with tenant_transaction(app_engine, lab) as conn:
        written = await record_laser_calibration(conn, lab, only, record)

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM laser_calibrations WHERE id = :i"), {"i": written}
            )
        ).one()
    assert (row.producer, row.outcome, row.dive_id) == (
        "checkerboard",
        "accepted",
        only,
    )
    assert row.laser_position == GOOD_POSITION and row.laser_axis == [0.0, 0.0, 1.0]
    assert (row.camera_calibration_id, row.calibration_target_id) == (
        calibration,
        target,
    )
    assert row.slate_template_id is None
    assert (row.lever_arm_m, row.observation_count, row.core_version) == (
        1.2,
        6,
        "4.1.0",
    )
    assert row.inputs_as_of == later(2)
    assert row.gate_verdicts == record.gate_verdicts


async def test_a_record_cannot_land_on_another_tenants_dive(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine), await tenant(owner_engine, "reef")
    theirs = await dive(owner_engine, reef)

    with pytest.raises(Exception, match="foreign key"):
        async with tenant_transaction(app_engine, lab) as conn:
            await record_laser_calibration(conn, lab, theirs, _accepted())


# ---------- inputs: stage 13 ----------


async def _slate_inputs(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return await slate_calibration_inputs(conn, lab, dive_id)


async def test_stage_13_inputs_carry_the_template_camera_and_observations(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    device, calibration = await device_with_camera(owner_engine, lab)
    slate = await slate_template(owner_engine)
    only = await dive(owner_engine, lab, slate=slate, device=device)
    frame = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, frame, completed=True,
                      reference_points=[[1.0, 2.0], [3.0, 4.0]], skipped_points=[1],
                      ls_updated_at=later(1))  # fmt: skip
    await laser_label(owner_engine, lab, frame, x=600.0, y=500.0,
                      ls_updated_at=later(2))  # fmt: skip

    inputs = await _slate_inputs(app_engine, lab, only)

    assert (inputs.slate_template_id, inputs.camera_calibration_id) == (
        slate,
        calibration,
    )
    assert inputs.camera_matrix == K
    assert inputs.template_points == [tuple(p) for p in TEMPLATE_POINTS]
    assert inputs.dpi == 300
    (observation,) = inputs.observations
    assert observation.capture_id == frame
    assert observation.reference_points == [(1.0, 2.0), (3.0, 4.0)]
    assert observation.skipped_points == [1]
    assert (observation.laser_x, observation.laser_y) == (600.0, 500.0)
    assert inputs.inputs_as_of == later(2), "the newest label, in LS's clock"


async def test_stage_13_walks_every_live_slate_label_like_v1(owner_engine, app_engine):
    """v1's activity walked every non-superseded slate label, neither
    completed- nor canonical-filtered (the cohort counts fewer); a label whose
    points do not fit the template is skipped by the fit, not here. Only a
    frame with no live dot has no observation."""
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, slate=await slate_template(owner_engine),
                      device=device)  # fmt: skip
    kept = []
    for completed, canonical in ((True, True), (False, True), (True, False)):
        frame = await capture(owner_engine, lab, only, canonical=canonical)
        await slate_label(owner_engine, lab, frame, completed=completed)
        await laser_label(owner_engine, lab, frame)
        kept.append(frame)
    dotless = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, dotless, completed=True)
    dead = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, dead, completed=True, superseded=True)
    await laser_label(owner_engine, lab, dead)

    inputs = await _slate_inputs(app_engine, lab, only)

    assert sorted(o.capture_id for o in inputs.observations) == sorted(kept)


async def test_each_frame_uses_its_lowest_live_laser_label(owner_engine, app_engine):
    """v2: v1 took `get_laser_label(image_id).first()` with no ordering, so a
    frame with two live dots could resolve either way; the lowest wins, as the
    checkerboard resolver already chose, so a re-dispatch fits the same
    points. A superseded label with a lower number never shadows a live one."""
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, slate=await slate_template(owner_engine),
                      device=device)  # fmt: skip
    frame = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, frame, completed=True)
    await laser_label(owner_engine, lab, frame, x=999.0, superseded=True, project=1,
                      v1_id=1)  # fmt: skip
    await laser_label(owner_engine, lab, frame, x=610.0, project=2, v1_id=9)
    await laser_label(owner_engine, lab, frame, x=600.0, project=3, v1_id=4)

    inputs = await _slate_inputs(app_engine, lab, only)

    assert [o.laser_x for o in inputs.observations] == [600.0]


async def test_the_dives_dots_are_every_live_dot_it_holds(owner_engine, app_engine):
    """v1's `get_laser_labels(dive)`: non-superseded with x/y set, incomplete
    and non-canonical included -- the population the describes-the-dive gate
    judges the fit against."""
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, slate=await slate_template(owner_engine),
                      device=device)  # fmt: skip
    await slate_label(owner_engine, lab, await capture(owner_engine, lab, only))
    await laser_label(owner_engine, lab, await capture(owner_engine, lab, only),
                      x=1.0, completed=False)  # fmt: skip
    await laser_label(owner_engine, lab,
                      await capture(owner_engine, lab, only, canonical=False), x=2.0)  # fmt: skip
    await laser_label(owner_engine, lab, await capture(owner_engine, lab, only),
                      x=3.0, superseded=True)  # fmt: skip
    await laser_label(owner_engine, lab, await capture(owner_engine, lab, only),
                      x=None, y=None)  # fmt: skip

    inputs = await _slate_inputs(app_engine, lab, only)

    assert sorted(x for x, _ in inputs.dive_dots) == [1.0, 2.0]


async def test_a_dive_with_no_slate_labels_has_nothing_to_calibrate(
    owner_engine, app_engine
):
    """v1's activity returned None (a genuine no-op) for a dive with no slate
    or no slate labels; the orchestrator now dispatches nothing."""
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    no_slate = await dive(owner_engine, lab, device=device)
    no_labels = await dive(owner_engine, lab, slate=await slate_template(owner_engine),
                           device=device)  # fmt: skip

    assert await _slate_inputs(app_engine, lab, no_slate) is None
    assert await _slate_inputs(app_engine, lab, no_labels) is None


async def test_stage_13_refuses_a_template_it_cannot_scale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, slate=await slate_template(owner_engine, dpi=None),
                      device=device)  # fmt: skip
    await _observation(owner_engine, lab, only)

    with pytest.raises(CalibrationInputsUnavailable, match="dpi or reference_points"):
        await _slate_inputs(app_engine, lab, only)


# ---------- inputs: the board ----------


async def _board_inputs(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return await checkerboard_calibration_inputs(conn, lab, dive_id)


async def _board_scene(owner_engine, lab, **target):
    device, calibration = await device_with_camera(owner_engine, lab)
    board = await calibration_target(owner_engine, **target)
    only = await dive(owner_engine, lab, target=board, device=device)
    return only, board, calibration


async def test_resolver_carries_the_board_geometry(owner_engine, app_engine):
    """The measured pitch reaches the child in the payload, read from the row,
    so a replayed child cannot pick up a different one. v2: per axis."""
    lab = await tenant(owner_engine)
    only, board, calibration = await _board_scene(
        owner_engine, lab, rows=10, cols=14, pitch_x_m=0.04223, pitch_y_m=0.04211
    )
    frames = []
    for n in (1, 2):
        frame = await capture(owner_engine, lab, only, checksum=f"{n:032x}")
        await laser_label(owner_engine, lab, frame, x=600.0 + n)
        frames.append(frame)

    inputs = await _board_inputs(app_engine, lab, only)

    assert (inputs.rows, inputs.cols) == (10, 14)
    assert (inputs.pitch_x_m, inputs.pitch_y_m) == (0.04223, 0.04211)
    assert (inputs.calibration_target_id, inputs.camera_calibration_id) == (
        board,
        calibration,
    )
    assert inputs.camera_matrix == K and inputs.distortion_coefficients == DISTORTION
    assert [f.capture_id for f in inputs.frames] == frames
    assert [f.checksum for f in inputs.frames] == [f"{1:032x}", f"{2:032x}"]
    assert [f.laser_x for f in inputs.frames] == [601.0, 602.0]


@pytest.mark.parametrize(
    ("label_kwargs", "why"),
    [
        ({"x": None, "y": None}, "populate-seeded placeholder, no dot"),
        ({"superseded": True}, "dead-lettered by the RANSAC validator"),
    ],
)
async def test_resolver_drops_unusable_laser_labels(
    owner_engine, app_engine, label_kwargs, why
):
    lab = await tenant(owner_engine)
    only, _, _ = await _board_scene(owner_engine, lab)
    kept = await capture(owner_engine, lab, only)
    await laser_label(owner_engine, lab, kept)
    await laser_label(
        owner_engine, lab, await capture(owner_engine, lab, only), **label_kwargs
    )

    inputs = await _board_inputs(app_engine, lab, only)

    assert [f.capture_id for f in inputs.frames] == [kept], why


async def test_resolver_drops_non_canonical_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only, _, _ = await _board_scene(owner_engine, lab)
    kept = await capture(owner_engine, lab, only)
    await laser_label(owner_engine, lab, kept)
    await laser_label(
        owner_engine, lab, await capture(owner_engine, lab, only, canonical=False)
    )

    inputs = await _board_inputs(app_engine, lab, only)

    assert [f.capture_id for f in inputs.frames] == [kept]


async def test_one_frame_per_image_when_an_image_has_two_live_labels(
    owner_engine, app_engine
):
    """The cohort counts IMAGES with a live dot; the resolver must agree, or a
    frame is decoded twice and double-weighted in the fit. Lowest label wins,
    deterministically; a superseded lower one does not shadow it."""
    lab = await tenant(owner_engine)
    only, _, _ = await _board_scene(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await laser_label(owner_engine, lab, frame, x=999.0, superseded=True, project=1,
                      v1_id=1)  # fmt: skip
    await laser_label(owner_engine, lab, frame, x=610.0, project=2, v1_id=9)
    await laser_label(owner_engine, lab, frame, x=600.0, project=3, v1_id=4)

    inputs = await _board_inputs(app_engine, lab, only)

    assert [f.laser_x for f in inputs.frames] == [600.0]


async def test_a_pitch_correction_reaches_the_next_fit(owner_engine, app_engine):
    """v2: the dive links one version row; the resolver re-reads the current
    version of that target by name, or dives would keep the stale pitch, and
    names the version it used."""
    lab = await tenant(owner_engine)
    name = f"E4E {uuid.uuid4()}"
    only, first, _ = await _board_scene(owner_engine, lab, name=name, pitch_x_m=0.042)
    corrected = await calibration_target(owner_engine, name=name, pitch_x_m=0.04223,
                                         valid_from=later(1))  # fmt: skip

    inputs = await _board_inputs(app_engine, lab, only)

    assert inputs.pitch_x_m == 0.04223
    assert inputs.calibration_target_id == corrected != first


async def test_resolver_refuses_an_unlinked_dive(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, device=device)

    with pytest.raises(CalibrationInputsUnavailable, match="no calibration target"):
        await _board_inputs(app_engine, lab, only)


async def test_resolver_refuses_a_dive_with_no_camera_calibration(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await dive(owner_engine, lab, target=await calibration_target(owner_engine))

    with pytest.raises(CalibrationInputsUnavailable, match="camera calibration"):
        await _board_inputs(app_engine, lab, only)


# ---------- the catalog, as the orchestrator's principal ----------


async def test_the_catalog_works_within_a_served_tenant(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    only = await _slate_dive(owner_engine, lab)
    catalog = LaserCalibrationCatalog(app_engine, sub=ORCHESTRATOR)

    assert (await catalog.next_dive_for_laser_calibration(lab)).dive_id == only
    assert await catalog.next_dive_for_checkerboard_calibration(lab) is None
    await catalog.record_laser_calibration(lab, only, _accepted())
    assert await catalog.next_dive_for_laser_calibration(lab) is None
    assert await catalog.clear_calibration_refusal(lab, only) is False
