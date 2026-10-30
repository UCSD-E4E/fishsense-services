"""The checked borrow (0082; the owner, 2026-10-29): an aim with no
calibration of its own may take a neighbor's slate-pose beam, when that beam
runs along the aim's own dot line and reads the aim's own posed slates at
their depth (`calibration_borrow`).

The API's two halves are pinned here; the check itself, on the posed points,
is the processor's (`automatic_calibration.borrow`), and the orchestrator
runs the three in turn:

* the candidate: accepted along the line (nothing written yet), refused off
  it or with no clear line (the refusal written); only a same-device,
  same-day slate-pose fit with an error bar, under the borrower's camera,
  lends, and with none nothing is tried or owed; the closest beam wins;
  nothing is borrowed for an aim that has a calibration;
* the check's verdict, written: an accepted borrow carries the lender's
  beam and its bar widened by the check, and serves the aim; a refused one
  serves nothing; either is a try, so the aim is owed again only for a
  lender's fit since; a lender that is not the aim's current one is refused;
* a borrow stops serving once its lender's fit is no longer current.
"""

from __future__ import annotations

import math
import uuid
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import text

from _slate_calibration_seed import beam_moved_after, slate_template
from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    T0,
    device,
    exec_,
    forget_identities,
    mission,
    tenant,
)
from fishsense_services_api.automatic_results_store import InvalidAutomaticResults
from fishsense_services_api.calibration_borrow import (
    BORROW_VERSION,
    BORROW_WORK,
    BorrowCheck,
    automatic_borrow_candidate,
    persist_automatic_borrow,
)
from test_automatic_results_store import _camera, _in, _rows
from test_calibration_per_aim import (
    BEAM,
    OTHER_BEAM,
    _dots,
    _frames,
    _measure,
    _persist,
)

pytestmark = pytest.mark.usefixtures("forget_identities")

BAR = [[1.0, 0.02], [3.0, 0.03]]


async def _lender(owner_engine, app_engine, lab, device_id, *, beam=OTHER_BEAM,
                  started=T0, method="slate_pose", **row):  # fmt: skip
    """A mission on `device_id` with an accepted slate-pose fit of `beam`."""
    lender = await mission(owner_engine, lab, device_id=device_id, priority="low")
    await exec_(owner_engine, "UPDATE missions SET started_at = :s WHERE id = :m",
                s=started, m=lender)  # fmt: skip
    accepted = row.get("outcome", "accepted") == "accepted"
    if method == "slate_pose":
        row = {"slate_template_id": await slate_template(owner_engine),
               "algorithm_version": "slate-pose-6", **row}  # fmt: skip
    return lender, await _persist(
        app_engine, lab, lender, method=method,
        camera_calibration_id=await _camera(app_engine, lab, lender),
        laser_position=beam[0] if accepted else None,
        laser_axis=beam[1] if accepted else None,
        scale_sd=0.02 if accepted and method == "slate_pose" else None,
        scale_sd_by_range=BAR if accepted and method == "slate_pose" else None,
        **row,
    )  # fmt: skip


async def _borrower(owner_engine, app_engine, lab, *, dots=None, device_id=None):
    """A moved mission whose second aim's dots are `dots` (OTHER_BEAM's by
    default), its own fit there refused: ``(mission, device, aim-1 frames)``."""
    device_id = device_id or await device(owner_engine, lab)
    mission_id = await mission(owner_engine, lab, device_id=device_id, priority="low")
    first = await _frames(owner_engine, app_engine, lab, mission_id, _dots(BEAM, 2))
    second = await _frames(owner_engine, app_engine, lab, mission_id,
                           _dots(OTHER_BEAM, 20, seed=2) if dots is None else dots)  # fmt: skip
    await beam_moved_after(owner_engine, first[-1])
    await _persist(app_engine, lab, mission_id)
    await _persist(app_engine, lab, mission_id, aim=1, outcome="refused",
                   refusal_reason="too_few_frames", laser_position=None,
                   laser_axis=None)  # fmt: skip
    return mission_id, device_id, second


async def _candidate(app_engine, lab, mission_id, aim=1):
    return await _in(app_engine, lab, automatic_borrow_candidate, mission_id,
                     aim=aim)  # fmt: skip


async def _check(app_engine, lab, mission_id, candidate, frames, *, ratio=0.03,
                 outcome="accepted", reason=None, aim=1):  # fmt: skip
    check = BorrowCheck(outcome=outcome, refusal_reason=reason, depth_ratio=ratio,
                        holds=2, capture_ids=list(frames[:4]))  # fmt: skip
    return await _in(app_engine, lab, persist_automatic_borrow, mission_id,
                     candidate, check, aim=aim)  # fmt: skip


async def _row(owner_engine, row_id):
    return (await _rows(owner_engine, """
        SELECT method, aim, outcome, refusal_reason, borrowed_from, borrow_line_px,
               borrow_depth_ratio, borrow_holds, laser_axis, scale_sd,
               scale_sd_by_range, algorithm_version, capture_ids
        FROM automatic_laser_calibrations WHERE id = :i""", i=row_id))[0]  # fmt: skip


async def _last_borrow(owner_engine, mission_id):
    return (await _rows(owner_engine, """
        SELECT id FROM automatic_laser_calibrations
        WHERE mission_id = :m AND method = 'borrowed' ORDER BY seq DESC LIMIT 1
        """, m=mission_id))[0]["id"]  # fmt: skip


async def _owes(conn, tenant_id, mission_id):
    return (await conn.execute(
        text(f"SELECT {BORROW_WORK} FROM missions d WHERE d.id = :m"),
        {"m": mission_id})).scalar_one()  # fmt: skip


# -- the candidate ---------------------------------------------------------------------


async def test_a_beam_along_the_aims_dot_line_is_the_candidate(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab)
    _, lent = await _lender(owner_engine, app_engine, lab, device_id)

    candidate = await _candidate(app_engine, lab, mission_id)

    assert candidate.lender_id == lent and candidate.line_px < 5
    assert candidate.laser_axis == OTHER_BEAM[1]
    # Nothing is written until the check comes back.
    assert not await _rows(owner_engine, "SELECT 1 FROM automatic_laser_calibrations "
                           "WHERE method = 'borrowed'")  # fmt: skip


async def test_a_beam_off_the_aims_line_is_refused(owner_engine, app_engine):
    """374's case: its beam 13-19 px off 383's after-move line."""
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(
        owner_engine, app_engine, lab, dots=_dots(OTHER_BEAM, 20, shift_px=20.0, seed=2)
    )
    _, lent = await _lender(owner_engine, app_engine, lab, device_id)

    assert await _candidate(app_engine, lab, mission_id) is None

    row = await _row(owner_engine, await _last_borrow(owner_engine, mission_id))
    assert (row["outcome"], row["refusal_reason"]) == ("refused", "no_matching_beam")
    assert row["borrowed_from"] == lent and row["borrow_line_px"] > 10
    assert (await _measure(app_engine, lab, mission_id, 1)).calibration is None


def _scattered(n, seed):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(0, 4000, n), rng.uniform(0, 3000, n)])


@pytest.mark.parametrize("dots", ["too_few", "short", "rival"])
async def test_an_aim_with_no_clear_line_is_refused(owner_engine, app_engine, dots):
    """Too few dots, too few of them on one line, or a second line half as
    supported (a reflection, wrong dots): nothing to match a beam against."""
    lab = await tenant(owner_engine)
    xy = {
        "too_few": lambda: _dots(OTHER_BEAM, 5, seed=2),
        "short": lambda: np.vstack([_dots(OTHER_BEAM, 6, seed=2), _scattered(4, 5)]),
        "rival": lambda: np.vstack([_dots(OTHER_BEAM, 14, seed=2),
                                    _dots(OTHER_BEAM, 8, shift_px=200.0, seed=3)]),
    }[dots]()  # fmt: skip
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab, dots=xy)
    await _lender(owner_engine, app_engine, lab, device_id)

    assert await _candidate(app_engine, lab, mission_id) is None

    row = await _row(owner_engine, await _last_borrow(owner_engine, mission_id))
    assert (row["outcome"], row["refusal_reason"]) == ("refused", "aim_line_unclear")


@pytest.mark.parametrize("lender", ["other_device", "days_apart", "refused",
                                    "size_constancy", "borrowed", "other_camera"])  # fmt: skip
async def test_only_a_same_device_same_day_slate_pose_fit_lends(
    owner_engine, app_engine, lender
):
    """A size-constancy fit has no error bar to widen: it lends nothing."""
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab)
    if lender == "other_device":
        await _lender(owner_engine, app_engine, lab, await device(owner_engine, lab))
    elif lender == "days_apart":
        await _lender(owner_engine, app_engine, lab, device_id,
                      started=T0 + timedelta(days=2))  # fmt: skip
    elif lender == "refused":
        await _lender(owner_engine, app_engine, lab, device_id, outcome="refused",
                      refusal_reason="too_few_frames")  # fmt: skip
    elif lender == "size_constancy":
        await _lender(owner_engine, app_engine, lab, device_id, method="size_constancy")
    elif lender == "borrowed":
        # A mission that borrowed lends nothing on (no chains).
        other, _, frames = await _borrower(owner_engine, app_engine, lab,
                                           device_id=device_id,
                                           dots=_dots(OTHER_BEAM, 20, seed=4))  # fmt: skip
        source, _ = await _lender(owner_engine, app_engine, lab, device_id)
        candidate = await _candidate(app_engine, lab, other)
        await _check(app_engine, lab, other, candidate, frames)
        await exec_(owner_engine, "UPDATE missions SET device_id = :d WHERE id = :m",
                    d=await device(owner_engine, lab), m=source)  # fmt: skip
    else:
        await _lender(owner_engine, app_engine, lab, device_id)
        await exec_(owner_engine, """
            INSERT INTO camera_calibrations (tenant_id, device_id, camera_model,
                port_model, camera_matrix, distortion_coefficients)
            SELECT tenant_id, device_id, camera_model, port_model, camera_matrix,
                   distortion_coefficients
            FROM camera_calibrations WHERE device_id = :d
            """, d=device_id)  # fmt: skip

    assert await _candidate(app_engine, lab, mission_id) is None
    assert not await _in(app_engine, lab, _owes, mission_id)


async def test_the_closest_beam_is_the_candidate(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab)
    near = [OTHER_BEAM[0], [0.0302, 0.045, 0.9985]]
    await _lender(owner_engine, app_engine, lab, device_id, beam=near)
    _, exact = await _lender(owner_engine, app_engine, lab, device_id)

    assert (await _candidate(app_engine, lab, mission_id)).lender_id == exact


async def test_nothing_is_borrowed_for_an_aim_with_a_calibration(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab)
    await _lender(owner_engine, app_engine, lab, device_id)

    assert await _candidate(app_engine, lab, mission_id, aim=0) is None  # its own fit
    await _persist(app_engine, lab, mission_id, aim=1, laser_axis=OTHER_BEAM[1])
    assert await _candidate(app_engine, lab, mission_id, aim=1) is None


# -- the check's verdict ---------------------------------------------------------------


async def test_a_checked_borrow_serves_the_aim_with_its_bar_widened(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, device_id, frames = await _borrower(owner_engine, app_engine, lab)
    _, lent = await _lender(owner_engine, app_engine, lab, device_id)
    candidate = await _candidate(app_engine, lab, mission_id)

    borrowed = await _check(app_engine, lab, mission_id, candidate, frames, ratio=-0.03)

    row = await _row(owner_engine, borrowed)
    assert {k: row[k] for k in ("method", "aim", "outcome", "borrowed_from",
                                "borrow_depth_ratio", "borrow_holds",
                                "algorithm_version")} == {
        "method": "borrowed", "aim": 1, "outcome": "accepted", "borrowed_from": lent,
        "borrow_depth_ratio": -0.03, "borrow_holds": 2,
        "algorithm_version": BORROW_VERSION,
    }  # fmt: skip
    assert row["borrow_line_px"] == round(candidate.line_px, 2)
    assert row["laser_axis"] == OTHER_BEAM[1]
    assert row["capture_ids"] == list(frames[:4])
    # The lender's bar, widened by what the check read (2026-10-29: on 17
    # kept borrows, 82-100% within this bar, all within two).
    assert row["scale_sd"] == pytest.approx(math.hypot(0.02, 0.03))
    assert np.allclose(row["scale_sd_by_range"],
                       [[1.0, math.hypot(0.02, 0.03)], [3.0, math.hypot(0.03, 0.03)]])  # fmt: skip
    cal = (await _measure(app_engine, lab, mission_id, 1)).calibration
    assert cal.automatic_laser_calibration_id == borrowed
    assert not await _in(app_engine, lab, _owes, mission_id)


@pytest.mark.parametrize("reason", ["slates_disagree", "too_few_posed",
                                    "no_slate_template"])  # fmt: skip
async def test_a_refused_check_serves_nothing_and_is_a_try(
    owner_engine, app_engine, reason
):
    lab = await tenant(owner_engine)
    mission_id, device_id, frames = await _borrower(owner_engine, app_engine, lab)
    await _lender(owner_engine, app_engine, lab, device_id)
    candidate = await _candidate(app_engine, lab, mission_id)

    refused = await _check(app_engine, lab, mission_id, candidate, frames,
                           outcome="refused", reason=reason,
                           ratio=0.09 if reason == "slates_disagree" else None)  # fmt: skip

    row = await _row(owner_engine, refused)
    assert (row["outcome"], row["refusal_reason"]) == ("refused", reason)
    assert (row["laser_axis"], row["scale_sd_by_range"]) == (None, None)
    assert (await _measure(app_engine, lab, mission_id, 1)).calibration is None
    assert not await _in(app_engine, lab, _owes, mission_id)


async def test_a_lender_that_is_not_the_aims_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, device_id, frames = await _borrower(owner_engine, app_engine, lab)
    await _lender(owner_engine, app_engine, lab, device_id)
    candidate = await _candidate(app_engine, lab, mission_id)
    _, elsewhere = await _lender(owner_engine, app_engine, lab,
                                 await device(owner_engine, lab))  # fmt: skip

    with pytest.raises(InvalidAutomaticResults, match="lender"):
        await _check(app_engine, lab, mission_id,
                     type(candidate)(**{**vars(candidate), "lender_id": elsewhere}),
                     frames)  # fmt: skip


async def test_a_borrow_stops_serving_when_its_lenders_fit_is_replaced(
    owner_engine, app_engine
):
    """...and the mission owes the borrow again, for the refit."""
    lab = await tenant(owner_engine)
    mission_id, device_id, frames = await _borrower(owner_engine, app_engine, lab)
    lender, _ = await _lender(owner_engine, app_engine, lab, device_id)
    await _check(app_engine, lab, mission_id,
                 await _candidate(app_engine, lab, mission_id), frames)  # fmt: skip
    owed = lambda: _in(app_engine, lab, _owes, mission_id)  # noqa: E731
    assert not await owed()

    template = (await _rows(owner_engine, "SELECT slate_template_id FROM "
                            "automatic_laser_calibrations WHERE mission_id = :m",
                            m=lender))[0]["slate_template_id"]  # fmt: skip
    refit = await _persist(app_engine, lab, lender, method="slate_pose",
                           slate_template_id=template, algorithm_version="slate-pose-6",
                           laser_axis=OTHER_BEAM[1], scale_sd=0.02,
                           scale_sd_by_range=BAR,
                           camera_calibration_id=await _camera(app_engine, lab, lender))  # fmt: skip

    assert (await _measure(app_engine, lab, mission_id, 1)).calibration is None
    assert await owed()
    assert (await _candidate(app_engine, lab, mission_id)).lender_id == refit


async def test_a_borrow_is_owed_once_per_lender_not_without_one(
    owner_engine, app_engine
):
    """A mission alone on its device that day is never held for a borrow;
    one is tried once a lender is current, and again only for a fit since."""
    lab = await tenant(owner_engine)
    mission_id, device_id, _ = await _borrower(owner_engine, app_engine, lab)
    owed = lambda: _in(app_engine, lab, _owes, mission_id)  # noqa: E731
    assert not await owed()
    assert await _candidate(app_engine, lab, mission_id) is None  # nothing to try

    lender, _ = await _lender(owner_engine, app_engine, lab, device_id, beam=BEAM)
    assert await owed()
    assert await _candidate(app_engine, lab, mission_id) is None  # off the line
    assert not await owed()

    await _lender(owner_engine, app_engine, lab, device_id)
    assert await owed()  # a fit since
