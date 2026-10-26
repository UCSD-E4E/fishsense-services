"""The automatic-results CPU stages: the label-free fit per dive, and lengths.

New in v2. The fit is fishsense-core's `size_constancy` (golden-tested in
test_size_constancy.py) run on the dive's slate frames: their rectified JPEGs
are read from Garage, SIFT is taken around each automatic dot, every pair is
registered, and the beam is fitted. Pinned here end to end on synthetic frames
of one textured object seen at known ranges, whose beam the stage must
recover. The dive line is fishsense-core's `fit_dive_line` through every
automatic dot of the dive (cscw used production's dive line, fitted the same
way), or, too few or unconfident, the frames' own dots (cscw's fallback).

The length is stage 14's geometry (fishsense-core `world_point`: the dot's
depth by the closest approach of the camera ray and the laser, the head and
tail placed on the fronto-parallel plane at that depth), as cscw
e2e_measurement/score.py `depth`/`length` compute it. The dot is gated on
`LaserPoint.is_measurable()`, as stage 14's now is: a dot behind the camera,
or one too near the laser's vanishing point to trust, is no observation.
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

    async def download_image(self, ref=None, url=None):
        return self.jpegs[ref.key if url is None else url]


def _calibration_input(zs, *, line_dots=None):
    texture = _texture()
    frames, jpegs = [], {}
    for z in zs:
        dot = _dot_at(z)
        key = f"tenants/{uuid.uuid4()}/preprocess_headtail_jpeg/{uuid.uuid4().hex}.JPG"
        jpegs[key] = _frame(texture, dot, 1.5 / z)
        frames.append(
            AutomaticCalibrationFrame(capture_id=uuid.uuid4(),
                                      jpeg_url=key,
                                      x=float(dot[0]), y=float(dot[1]))  # fmt: skip
        )
    payload = FitAutomaticCalibrationInput(
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), camera_matrix=K.tolist(),
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


async def test_the_mission_line_comes_from_every_automatic_dot():
    """Given the dive's dots, their line is used, not the frames' own."""
    turn = np.radians(1.0)
    rot = np.array([[np.cos(turn), -np.sin(turn)], [np.sin(turn), np.cos(turn)]])
    # Turned about the frames' middle dot: a dive's line passes through its
    # calibration dots (they are among its dots), at a slightly different angle.
    pivot = _dot_at(2.0)
    line_dots = [
        list(pivot + rot @ (_dot_at(z) - pivot)) for z in np.linspace(0.8, 5.0, 40)
    ]
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10), line_dots=line_dots)

    r = await _fit(payload, store)

    direction = [*r.line_direction, 0]
    mission = np.subtract(line_dots[0], line_dots[-1])
    frames = _dot_at(1.0) - _dot_at(5.0)
    assert _angle(direction, [*mission, 0]) < 0.01
    assert _angle(direction, [*frames, 0]) > 0.9


def _off_line(payload, store, zs, offset_px):
    """Frames whose dot is `offset_px` off the dive line, sized as if their
    dot were on the object at a range unrelated to where it sits on the line
    (session 77's block of dots 35-41 px off its line, 2026-10-19)."""
    texture = _texture()
    u = _dot_at(1.0) - _dot_at(3.0)
    n = np.array([-u[1], u[0]]) / np.hypot(*u)
    extra = []
    for z, size in zs:
        dot = _dot_at(z) + offset_px * n
        key = f"tenants/{uuid.uuid4()}/preprocess_headtail_jpeg/{uuid.uuid4().hex}.JPG"
        store.jpegs[key] = _frame(texture, dot, size)
        extra.append(
            AutomaticCalibrationFrame(capture_id=uuid.uuid4(),
                                      jpeg_url=key,
                                      x=float(dot[0]), y=float(dot[1]))  # fmt: skip
        )
    return payload.model_copy(update={"frames": [*payload.frames, *extra]}), extra


_MISSION_DOTS = [list(_dot_at(z)) for z in np.linspace(0.8, 5.0, 40)]
_OFF = [(1.2, 0.7), (1.6, 1.4), (2.0, 0.5), (2.4, 1.3), (2.8, 0.6), (1.4, 1.1)]


async def test_dots_off_the_mission_line_are_not_fitted():
    """A dot 38 px off the dive line is not the laser on the slate at that
    range (a reflection, or the detector on the slate's pattern); fitted, it
    breaks the size-range relation the fit rests on."""
    payload, store = _calibration_input(
        np.linspace(1.0, 3.0, 10), line_dots=_MISSION_DOTS
    )
    payload, off = _off_line(payload, store, _OFF, 38.0)

    r = await _fit(payload, store)

    assert r.outcome == "accepted", r.refusal_reason
    assert r.candidate_count == 16
    assert not set(r.capture_ids) & {f.capture_id for f in off}
    assert _angle(r.laser_axis, TRUE_D) < 0.1


async def test_without_a_mission_line_the_frames_own_line_gates_them():
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10))
    payload, off = _off_line(payload, store, _OFF, 38.0)

    r = await _fit(payload, store)

    assert r.outcome == "accepted", r.refusal_reason
    assert not set(r.capture_ids) & {f.capture_id for f in off}
    assert _angle(r.laser_axis, TRUE_D) < 0.1


@pytest.mark.parametrize(
    "line_dots, fitted_with_a_line",
    [(_MISSION_DOTS, True), ([], False)],
    ids=["mission-line", "no-mission-line"],
)
async def test_with_no_mission_line_the_fit_takes_cscws_fallback(
    monkeypatch, line_dots, fitted_with_a_line
):
    """The frames' own RANSAC line only gates the dots; the fit itself, with
    no mission line, takes its direction through the dots it uses
    (`fit_label_free(line=None)`, cscw's fallback) -- as before the gate
    (review of #136)."""
    from fishsense_core import size_constancy

    seen = []
    real = size_constancy.fit_label_free
    monkeypatch.setattr(size_constancy, "fit_label_free",
                        lambda **kw: seen.append(kw["line"]) or real(**kw))  # fmt: skip
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10), line_dots=line_dots)
    payload, off = _off_line(payload, store, _OFF, 38.0)

    r = await _fit(payload, store)

    assert (seen[0] is not None) == fitted_with_a_line
    assert not set(r.capture_ids) & {f.capture_id for f in off}


async def test_dots_a_few_px_off_the_line_are_kept():
    """Detector jitter is a pixel or two (automatic vs human dots, 2026-10-19)."""
    payload, store = _calibration_input(
        np.linspace(1.0, 3.0, 10), line_dots=_MISSION_DOTS
    )
    payload, near = _off_line(payload, store, [(1.5, 1.5 / 1.5), (2.5, 1.5 / 2.5)], 3.0)

    r = await _fit(payload, store)

    assert {f.capture_id for f in near} <= set(r.capture_ids)


async def test_a_second_line_of_dots_makes_the_line_ambiguous():
    """Session 77 (2026-10-19): a reflection's dots formed a line of their own,
    30 dots to the laser's 48 by the dive line's count -- and the dive line had
    taken the wrong one (1.35 deg from the line fitted from labels). Without
    labels the two cannot be told apart, so the fit refuses."""
    payload, store = _calibration_input(
        np.linspace(1.0, 3.0, 10), line_dots=_MISSION_DOTS
    )
    second = [(z, 1.5 / z) for z in np.linspace(1.1, 2.9, 9)]
    payload, _ = _off_line(payload, store, second, 38.0)

    r = await _fit(payload, store)

    assert (r.outcome, r.refusal_reason) == ("refused", "ambiguous_line")
    assert r.candidate_count == 19


async def test_every_dot_off_the_line_is_a_refusal():
    payload, store = _calibration_input([], line_dots=_MISSION_DOTS)
    payload, _ = _off_line(payload, store, _OFF * 2, 38.0)

    r = await _fit(payload, store)

    assert (r.outcome, r.refusal_reason) == ("refused", "too_few_frames")
    assert r.candidate_count == 12


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
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), camera_matrix=K.tolist(),
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


async def test_a_dot_near_the_vanishing_point_is_refused_as_low_parallax():
    """core #96: the laser's own hit at 50 m triangulates to a finite,
    positive depth (~50 m) that version 1's `depth > 0` gate measured, but
    its camera ray meets the laser at ~0.002 rad, under fishsense-core's
    0.005 rad: a pixel of dot error moves that depth by more than 7 %."""
    r = await ActivityEnvironment().run(
        measure_automatic, _measure_input(_capture(_dot_at(50.0)))
    )
    (got,) = r.lengths
    assert (got.length_m, got.refusal) == (None, "low_parallax")
    assert got.depth_m == pytest.approx(50.0, rel=0.1)


def test_the_measurement_version_is_2_since_the_measurable_gate():
    """2 (2026-10-23): the `is_measurable` gate refuses low-parallax dots that
    version 1 measured, so every version-1 length is measured again."""
    assert AUTOMATIC_MEASUREMENT_VERSION == "2"


async def test_a_zero_length_is_refused():
    c = _capture(_dot_at(2.0), head=(2000.0, 1500.0), tail=(2000.0, 1500.0))
    r = await ActivityEnvironment().run(measure_automatic, _measure_input(c))
    assert r.lengths[0].refusal == "zero_length"


# -- slate-heavy dives: a bounded fit that keeps heartbeating -----------------------

from fishsense_services_processor.automatic_calibration import (  # noqa: E402
    activities as cal,
)


def test_a_spread_keeps_the_extremes_and_evenly_covers_the_dot_travel():
    dots = np.array([_dot_at(z) for z in np.linspace(1.0, 3.0, 50)])
    picked = cal.spread(dots, 10)
    assert len(picked) == 10 and len(set(picked)) == 10
    assert 0 in picked and 49 in picked
    assert cal.spread(dots[:7], 10) == list(range(7))


async def test_a_slate_heavy_mission_is_fitted_on_a_capped_spread(monkeypatch):
    """2026-10-15 audit: registration is quadratic in frames (100 slate
    frames are 4,950 pairs) and a calibration session can hold hundreds; the
    fit takes at most `MAX_CALIBRATION_FRAMES`, spread along the dot's travel
    so the range the fit needs is kept."""
    monkeypatch.setattr(cal, "MAX_CALIBRATION_FRAMES", 12)
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 20))

    r = await _fit(payload, store)

    assert r.outcome == "accepted", r.refusal_reason
    assert r.candidate_count == 20 and r.frames_used <= 12
    assert r.size_ratio == pytest.approx(3.0, rel=0.05)
    assert _angle(r.laser_axis, TRUE_D) < 0.1


async def test_a_long_fit_keeps_heartbeating(monkeypatch):
    """The fit runs in a thread for minutes on a big dive; with no heartbeat
    the 5-minute heartbeat timeout killed it three times and stuck the dive."""
    import time

    real = cal._fit
    monkeypatch.setattr(cal, "HEARTBEAT_EVERY_S", 0.05)
    monkeypatch.setattr(cal, "_fit", lambda *a: (time.sleep(0.6), real(*a))[1])
    payload, store = _calibration_input(np.linspace(1.0, 3.0, 10))
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    acts = AutomaticCalibrationActivities(store_factory=lambda: store)

    await env.run(acts.fit_automatic_calibration, payload)

    # One per frame read, and then many while the fit ran.
    assert len(beats) >= len(payload.frames) + 5
