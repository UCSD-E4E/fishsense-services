"""The automatic-results CPU stages: the label-free fit per dive, and lengths.

New in v2. The fit is `automatic_results.size_constancy` (golden-tested in
test_size_constancy.py) run on the dive's slate frames: their rectified JPEGs
are read from Garage, SIFT is taken around each automatic dot, every pair is
registered, and the beam is fitted. Pinned here end to end on synthetic frames
of one textured object seen at known ranges, whose beam the stage must
recover. The dive line is fishsense-core's `fit_dive_line` through every
automatic dot of the dive (cscw used production's dive line, fitted the same
way), or, too few or unconfident, the frames' own dots (cscw's fallback).

The length is stage 14's geometry (`laser_geometry`: the dot's depth by the
closest approach of the camera ray and the laser, the head and tail placed on
the fronto-parallel plane at that depth), as cscw e2e_measurement/score.py
`depth`/`length` compute it; unlike stage 14 the depth is gated (> 0), since
an automatic dot behind the camera is no observation (laser_geometry's own
advice; stage 14 keeps v1's parity).
"""

from __future__ import annotations

import uuid

import cv2
import numpy as np
import pytest

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_CALIBRATION_VERSION,
    AUTOMATIC_MEASUREMENT_ALGORITHM,
    AUTOMATIC_MEASUREMENT_VERSION,
    AutomaticCalibrationFrame,
    AutomaticMeasureCapture,
    FitAutomaticCalibrationInput,
    MeasureAutomaticInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_processor.automatic_calibration.activities import (
    AutomaticCalibrationActivities,
)
from fishsense_services_processor.automatic_measurement.activities import (
    measure_automatic,
)
from temporalio.testing import ActivityEnvironment

K = np.array([[2850.0, 0.0, 2000.0], [0.0, 2850.0, 1500.0], [0.0, 0.0, 1.0]])
TRUE_O = np.append(0.104 * np.array([-0.29, -0.957]) / np.hypot(0.29, 0.957), 0.0)
TRUE_D = np.array([0.012, 0.035, 1.0])
W, H = 4000, 3000


def _dot_at(z):
    p = TRUE_O + (z - TRUE_O[2]) / TRUE_D[2] * TRUE_D
    q = K @ (p / p[2])
    return q[:2]


def _angle(a, b):
    a, b = np.asarray(a) / np.linalg.norm(a), np.asarray(b) / np.linalg.norm(b)
    return float(np.degrees(np.arccos(np.clip(a @ b, -1, 1))))


def _texture():
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 255, size=(H // 8, W // 8), dtype=np.uint8)
    return cv2.resize(noise, (W, H), interpolation=cv2.INTER_CUBIC)


def _frame(texture, dot, scale):
    """The object, scaled by `scale` about its own centre, which lands on the dot."""
    m = cv2.getRotationMatrix2D((W / 2, H / 2), 0.0, scale)
    m[:, 2] += np.asarray(dot) - (W / 2, H / 2)
    img = cv2.warpAffine(texture, m, (W, H))
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    assert ok
    return buf.tobytes()


class _Store:
    def __init__(self, jpegs):
        self.jpegs = jpegs

    async def download_processed_jpeg(self, ref):
        return self.jpegs[ref.key]


def _calibration_input(zs, *, line_dots=None):
    texture = _texture()
    frames, jpegs = [], {}
    for z in zs:
        dot = _dot_at(z)
        key = f"tenants/{uuid.uuid4()}/preprocess_headtail_jpeg/{uuid.uuid4().hex}.JPG"
        jpegs[key] = _frame(texture, dot, 1.5 / z)
        frames.append(
            AutomaticCalibrationFrame(capture_id=uuid.uuid4(),
                                      jpeg=ObjectRef(bucket="b", key=key),
                                      x=float(dot[0]), y=float(dot[1]))  # fmt: skip
        )
    payload = FitAutomaticCalibrationInput(
        tenant_id=uuid.uuid4(), dive_id=uuid.uuid4(), camera_matrix=K.tolist(),
        frames=frames, line_dots=line_dots or [],
    )  # fmt: skip
    return payload, _Store(jpegs)


async def _fit(payload, store):
    acts = AutomaticCalibrationActivities(store_factory=lambda: store)
    return await ActivityEnvironment().run(acts.fit_automatic_calibration, payload)


# -- the label-free fit, end to end ------------------------------------------------


async def test_recovers_the_beam_from_frames_of_an_unknown_object():
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10))

    r = await _fit(payload, store)

    assert r.outcome == "accepted", r.refusal_reason
    assert r.algorithm_version == AUTOMATIC_CALIBRATION_VERSION
    assert _angle(r.laser_axis, TRUE_D) < 0.1
    assert np.linalg.norm(r.laser_position) == pytest.approx(0.104)
    assert r.size_ratio == pytest.approx(3.0, rel=0.05)
    assert r.candidate_count == 10 and r.frames_used >= 9
    assert len(r.capture_ids) == r.frames_used
    assert set(r.capture_ids) <= {f.capture_id for f in payload.frames}
    assert r.core_version


async def test_the_dive_line_comes_from_every_automatic_dot():
    """Given the dive's dots, their line is used, not the frames' own."""
    turn = np.radians(1.0)
    rot = np.array([[np.cos(turn), -np.sin(turn)], [np.sin(turn), np.cos(turn)]])
    line_dots = [list(rot @ _dot_at(z)) for z in np.linspace(0.8, 5.0, 40)]
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10), line_dots=line_dots)

    r = await _fit(payload, store)

    direction = [*r.line_direction, 0]
    dive = np.subtract(line_dots[0], line_dots[-1])
    frames = _dot_at(1.0) - _dot_at(5.0)
    assert _angle(direction, [*dive, 0]) < 0.01
    assert _angle(direction, [*frames, 0]) > 0.9


async def test_no_slate_frames_is_a_refusal():
    payload, store = _calibration_input([])
    r = await _fit(payload, store)
    assert (r.outcome, r.refusal_reason, r.candidate_count) == (
        "refused", "no_candidates", 0
    )  # fmt: skip


async def test_too_little_range_is_a_refusal():
    payload, store = _calibration_input(np.linspace(2.0, 2.4, 10))
    r = await _fit(payload, store)
    assert (r.outcome, r.refusal_reason) == ("refused", "too_little_range_spread")
    assert r.laser_axis is None


# -- lengths -----------------------------------------------------------------------


def _cscw_depth(k, o, a, x, y):
    """cscw-fishsense2027@96a8da07 e2e_measurement/score.py `depth`."""
    d = np.linalg.solve(k, [x, y, 1.0])
    a = a / np.linalg.norm(a)
    m = np.array([[d @ d, -d @ a], [d @ a, -a @ a]])
    s, t = np.linalg.solve(m, np.array([d @ o, a @ o]))
    return float((o + t * a)[2])


def _cscw_length(k, z, hx, hy, tx, ty):
    """cscw-fishsense2027@96a8da07 e2e_measurement/score.py `length`."""
    ki = np.linalg.inv(k)
    return float(np.linalg.norm(ki @ [hx, hy, 1.0] * z - ki @ [tx, ty, 1.0] * z))


def _measure_input(*captures):
    return MeasureAutomaticInput(
        tenant_id=uuid.uuid4(), dive_id=uuid.uuid4(), camera_matrix=K.tolist(),
        laser_position=TRUE_O.tolist(), laser_axis=TRUE_D.tolist(),
        captures=list(captures),
    )  # fmt: skip


def _capture(dot, head=(1700.0, 1400.0), tail=(2300.0, 1450.0)):
    return AutomaticMeasureCapture(
        capture_id=uuid.uuid4(), automatic_head_tail_prediction_id=uuid.uuid4(),
        laser_x=float(dot[0]), laser_y=float(dot[1]),
        head_x=head[0], head_y=head[1], tail_x=tail[0], tail_y=tail[1],
    )  # fmt: skip


async def test_a_length_is_cscws_length():
    c = _capture(_dot_at(2.0))
    r = await ActivityEnvironment().run(measure_automatic, _measure_input(c))

    (got,) = r.lengths
    z = _cscw_depth(K, TRUE_O, TRUE_D, c.laser_x, c.laser_y)
    # fishsense-core solves in float32: its noise floor is ~1e-5 m at 2 m.
    assert got.depth_m == pytest.approx(2.0, rel=1e-4)
    assert got.depth_m == pytest.approx(z, rel=1e-4)
    assert got.length_m == pytest.approx(
        _cscw_length(K, z, c.head_x, c.head_y, c.tail_x, c.tail_y), rel=1e-4
    )
    assert got.refusal is None
    assert (r.algorithm, r.algorithm_version) == (
        AUTOMATIC_MEASUREMENT_ALGORITHM, AUTOMATIC_MEASUREMENT_VERSION
    )  # fmt: skip


async def test_a_dot_behind_the_camera_is_refused():
    """A dot on the far side of the vanishing point triangulates behind the
    camera: no observation, so no length (stage 14 keeps v1's sign parity)."""
    behind = _dot_at(2.0) + 4.0 * (_dot_at(50.0) - _dot_at(2.0))
    r = await ActivityEnvironment().run(
        measure_automatic, _measure_input(_capture(behind))
    )
    (got,) = r.lengths
    assert (got.length_m, got.refusal) == (None, "non_positive_depth")


async def test_a_zero_length_is_refused():
    c = _capture(_dot_at(2.0), head=(2000.0, 1500.0), tail=(2000.0, 1500.0))
    r = await ActivityEnvironment().run(measure_automatic, _measure_input(c))
    assert r.lengths[0].refusal == "zero_length"
