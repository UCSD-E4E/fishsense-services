"""A calibration's error bar, carried into its lengths (0079; the owner,
2026-10-26: "always show the length", weak signals widen the error rather
than refuse).

The slate-pose fit records its length scale's SD (`scale_sd`). A length on
that calibration carries ``length_m * scale_sd`` as its calibration error
(`length_sd_m`): the part of its error the calibration explains, not the
head/tail's. Pinned here:

* an accepted calibration may carry its SD, and only an accepted one;
* the frame view, the lengths list and the mission view show it;
* a length on a calibration without one (size constancy) has none, rather
  than a made-up zero.
"""

import json
import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from depth_measure_seed import capture, forget_identities, tenant  # noqa: F401
from fishsense_services_api.automatic_lengths_store import automatic_lengths
from fishsense_services_api.automatic_results_store import (
    AutomaticMeasurementRow,
    persist_automatic_calibration,
    persist_automatic_measurements,
)
from fishsense_services_api.automatic_review_store import (
    automatic_frame,
    mission_automatic,
)
from test_automatic_results_store import (
    _backlog_mission,
    _in,
    _label_free,
    _measure_inputs,
    _predict,
    _predicted,
)
from test_automatic_review_api import _lab, _number
from test_automatic_slate_pose import _posed, _with_template
from test_observations_api import _get, client  # noqa: F401

pytestmark = pytest.mark.usefixtures("forget_identities")

LENGTH_M = 0.40


async def _measured(owner_engine, app_engine, *, calibration, lab=None):
    """A mission with one fish measured at `LENGTH_M` on the calibration
    `calibration(template)` makes."""
    lab = lab or await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    c = await capture(owner_engine, lab, mission_id)
    await _predict(app_engine, lab, mission_id, _predicted(c))
    await _in(app_engine, lab, persist_automatic_calibration, mission_id,
              calibration(template))  # fmt: skip
    inputs = await _measure_inputs(app_engine, lab, mission_id)
    (m,) = inputs.captures
    await _in(app_engine, lab, persist_automatic_measurements, mission_id, [
        AutomaticMeasurementRow(
            capture_id=c,
            automatic_head_tail_prediction_id=m.automatic_head_tail_prediction_id,
            automatic_keypoint_prediction_id=m.automatic_keypoint_prediction_id,
            calibration_source=inputs.calibration.source,
            automatic_laser_calibration_id=inputs.calibration.automatic_laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
            length_m=LENGTH_M, depth_m=2.0, algorithm="laser_depth_fronto_parallel",
            algorithm_version="2", core_version="4.1.0",
        )
    ])  # fmt: skip
    return lab, mission_id, c


async def test_a_slate_pose_length_carries_its_calibration_error(
    owner_engine, app_engine
):
    lab, mission_id, c = await _measured(
        owner_engine, app_engine, calibration=lambda t: _posed(t, scale_sd=0.025)
    )
    frame = await _in(app_engine, lab, automatic_frame, c)
    assert math.isclose(frame.length_sd_m, LENGTH_M * 0.025)

    (length,) = (await _in(app_engine, lab, automatic_lengths)).lengths
    assert math.isclose(length.length_sd_m, LENGTH_M * 0.025)

    mission = await _in(app_engine, lab, mission_automatic, mission_id)
    assert mission.calibration.method == "slate_pose"
    assert math.isclose(mission.calibration.scale_sd, 0.025)


async def test_a_length_without_an_error_bar_says_none(owner_engine, app_engine):
    lab, mission_id, c = await _measured(
        owner_engine, app_engine, calibration=lambda t: _label_free()
    )
    assert (await _in(app_engine, lab, automatic_frame, c)).length_sd_m is None
    (length,) = (await _in(app_engine, lab, automatic_lengths)).lengths
    assert length.length_sd_m is None
    mission = await _in(app_engine, lab, mission_automatic, mission_id)
    assert mission.calibration.scale_sd is None


async def test_the_error_bar_is_served(
    client, seed_memberships, owner_engine, app_engine
):
    lab = await _lab(seed_memberships)
    _, mission_id, c = await _measured(owner_engine, app_engine, lab=lab,
                                       calibration=lambda t: _posed(t, scale_sd=0.025))  # fmt: skip
    number = await _number(owner_engine, mission_id)

    (row,) = (await _get(client, "/tenants/lab/automatic/lengths")).json()["lengths"]
    assert math.isclose(row["length_sd_m"], LENGTH_M * 0.025)
    frame = (await _get(client, f"/tenants/lab/captures/{c}/automatic")).json()
    assert math.isclose(frame["length_sd_m"], LENGTH_M * 0.025)
    mission = (await _get(client, f"/tenants/lab/missions/{number}/automatic")).json()
    assert math.isclose(mission["calibration"]["scale_sd"], 0.025)


@pytest.mark.parametrize(
    "row",
    [
        dict(scale_sd=0.0),
        dict(scale_sd=-0.01),
        dict(scale_sd=0.02, outcome="refused", refusal_reason="too_few_frames",
             laser_position=None, laser_axis=None),
    ],
    ids=["zero", "negative", "on_a_refusal"],
)  # fmt: skip
async def test_only_an_answer_carries_a_positive_error_bar(
    owner_engine, app_engine, row
):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    with pytest.raises(IntegrityError, match="scale_sd_check"):
        await _in(app_engine, lab, persist_automatic_calibration, mission_id,
                  _posed(template, **row))  # fmt: skip


# -- at each range (0080, 2026-10-27) ------------------------------------------------------

#: The bar at 1 m and 3 m; `_measured`'s fish is at 2 m, halfway.
BY_RANGE = [[1.0, 0.02], [3.0, 0.04]]


@pytest.mark.parametrize(
    "by_range,depth,want",
    [
        (BY_RANGE, 2.0, 0.03),
        (BY_RANGE, 1.0, 0.02),
        (BY_RANGE, 0.5, 0.02),
        (BY_RANGE, 4.0, 0.04),
        (None, 2.0, 0.05),
        (BY_RANGE, None, 0.05),
    ],
    ids=["between", "at_a_range", "nearer", "further", "no_ranges", "no_depth"],
)
async def test_the_bar_is_read_at_a_depth(owner_engine, by_range, depth, want):
    """Linear between the ranges given, the nearest one's beyond them, and
    the calibration's one SD where it gives none or the length has no
    depth."""
    async with owner_engine.connect() as conn:
        got = (await conn.execute(
            text("SELECT calibration_sd_at(CAST(:r AS jsonb), 0.05, :d)"),
            {"r": None if by_range is None else json.dumps(by_range), "d": depth},
        )).scalar_one()  # fmt: skip
    assert math.isclose(got, want)


async def test_a_length_takes_the_bar_at_its_own_depth(owner_engine, app_engine):
    """279 (2026-10-27): its beam was right at 2.5 m and +63% at 0.75 m, so
    one SD for every length was the wrong bar for most of them."""
    lab, mission_id, c = await _measured(
        owner_engine, app_engine,
        calibration=lambda t: _posed(t, scale_sd=0.04, scale_sd_by_range=BY_RANGE),
    )  # fmt: skip
    frame = await _in(app_engine, lab, automatic_frame, c)
    assert math.isclose(frame.length_sd_m, LENGTH_M * 0.03)
    (length,) = (await _in(app_engine, lab, automatic_lengths)).lengths
    assert math.isclose(length.length_sd_m, LENGTH_M * 0.03)
    mission = await _in(app_engine, lab, mission_automatic, mission_id)
    assert mission.calibration.scale_sd_by_range == BY_RANGE


@pytest.mark.parametrize(
    "row",
    [
        dict(scale_sd=0.02, scale_sd_by_range={"1.0": 0.02}),
        dict(scale_sd=0.02, scale_sd_by_range=BY_RANGE, outcome="refused",
             refusal_reason="too_few_frames", laser_position=None, laser_axis=None),
    ],
    ids=["not_an_array", "on_a_refusal"],
)  # fmt: skip
async def test_only_an_answer_carries_a_bar_by_range(owner_engine, app_engine, row):
    lab = await tenant(owner_engine)
    mission_id = await _backlog_mission(owner_engine, lab)
    template = await _with_template(owner_engine, mission_id)
    with pytest.raises(IntegrityError, match="scale_sd_by_range_check"):
        await _in(app_engine, lab, persist_automatic_calibration, mission_id,
                  _posed(template, **row))  # fmt: skip
