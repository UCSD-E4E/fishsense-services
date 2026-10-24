"""The stored refusal text, byte for byte, for every gate branch.

A refused fit's `refusal_reason` is written to `laser_calibrations` and read
back from prod by operators; it is the only thing they see. When the gates
moved from this package (`calibration/consistency.py`) into
`fishsense_core.calibration_checks` (fishsense-core 4.3.0, core #99), the
remedy paragraph moved out of core and became the caller's `remedies=`
argument. These pin the whole message each branch produces through
`fit_laser` -- the call site, not the gate -- so dropping `remedies=` from one
gate, or a wording drift in core, fails here rather than in prod rows.

The expected strings were captured from services' own gates (v0.51.0, before
the port) on these exact inputs, then left unchanged across the switch.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration import fit as fit_module

from ._calibration_fixtures import dots_on_projected_ray, mission_dots_on_ray

K = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]
#: 10.4 cm, where every sound calibration in the fleet sits.
PLAUSIBLE = (0.0624, 0.0832, 0.0)
#: 2.35 cm, dive 522's real fitted baseline.
IMPLAUSIBLE = (0.0141, 0.0188, 0.0)
#: The plausible offset turned 90 degrees: its image line is perpendicular.
QUARTER_TURN = (0.0832, -0.0624, 0.0)
DOWN = (0.0, 0.0, 1.0)
DEPTHS = 1.0 + 0.1 * np.arange(12)

_REMEDIES = (
    "The refusal is recorded, so this dive has left the calibration cohort "
    "and nothing is retrying it. It returns on its own once any laser or "
    "slate label on it is newer than the refusal, or its slate template or "
    "calibration target changes, so relabelling is enough and there is no "
    "flag to clear. If its observations cannot be fixed, park it with "
    "priority 'none' and a note; for a checkerboard dive that needs "
    "different frames rather than better labels, clear its "
    "calibration_target_id."
)


def _points(depths=DEPTHS) -> np.ndarray:
    depths = np.asarray(depths, dtype=float)
    return np.column_stack(
        [0.01 * np.arange(len(depths)), np.full(len(depths), 0.02), depths]
    )


def _p90_mission_dots():
    dots = np.asarray(mission_dots_on_ray(PLAUSIBLE, DOWN, K))
    shifted = np.asarray(mission_dots_on_ray(PLAUSIBLE, DOWN, K, offset_px=40.0))
    dots[-8:] = shifted[-8:]
    return [tuple(d) for d in dots]


#: name -> (kernel origin, kernel axis, points, dots, mission dots)
CASES = {
    "observation_geometry": (
        PLAUSIBLE,
        DOWN,
        _points(np.full(12, 1.40)),
        dots_on_projected_ray(PLAUSIBLE, DOWN, K, DEPTHS),
        mission_dots_on_ray(PLAUSIBLE, DOWN, K),
    ),
    "self_consistency_angle": (
        PLAUSIBLE,
        DOWN,
        _points(),
        dots_on_projected_ray(QUARTER_TURN, DOWN, K, DEPTHS),
        mission_dots_on_ray(PLAUSIBLE, DOWN, K),
    ),
    "self_consistency_offset": (
        PLAUSIBLE,
        DOWN,
        _points(),
        np.asarray(mission_dots_on_ray(PLAUSIBLE, DOWN, K, DEPTHS, offset_px=20.0)),
        mission_dots_on_ray(PLAUSIBLE, DOWN, K),
    ),
    "baseline": (
        IMPLAUSIBLE,
        DOWN,
        _points(),
        dots_on_projected_ray(IMPLAUSIBLE, DOWN, K, DEPTHS),
        mission_dots_on_ray(IMPLAUSIBLE, DOWN, K),
    ),
    "describes_dive_median": (
        PLAUSIBLE,
        DOWN,
        _points(),
        dots_on_projected_ray(PLAUSIBLE, DOWN, K, DEPTHS),
        mission_dots_on_ray(PLAUSIBLE, DOWN, K, offset_px=40.0),
    ),
    "describes_dive_p90": (
        PLAUSIBLE,
        DOWN,
        _points(),
        dots_on_projected_ray(PLAUSIBLE, DOWN, K, DEPTHS),
        _p90_mission_dots(),
    ),
}

EXPECTED = {
    "baseline": (
        "CalibrationImplausibleError",
        "fitted laser baseline 2.35 cm is outside the plausible range "
        "9.7-14.5 cm. The baseline is a property of the camera+laser rig, "
        "not of the dive, and the fleet sits at ~10.4 cm; a value this far "
        "off means the fit, not the hardware. It would scale every depth "
        "and every length by that factor, invisibly to reprojection "
        "residual and to the self-consistency gate. Refusing to persist. " + _REMEDIES,
    ),
    "describes_dive_median": (
        "CalibrationDoesNotDescribeDiveError",
        "calibration does not describe its dive: the fitted ray projects "
        "with median offset 40.0px from the dive's own 40 laser dots (gate "
        "6.0px, p90 40.0px). The laser was not in the same state for the "
        "frames being measured as for the frames it was calibrated from, or"
        " the fit came from too few observations to constrain it. Refusing "
        "to persist. " + _REMEDIES,
    ),
    "describes_dive_p90": (
        "CalibrationDoesNotDescribeDiveError",
        "calibration does not describe its dive: p90 offset 40.0px from the"
        " dive's own 40 laser dots (gate 12.0px, median 0.0px), with 8 of "
        "them beyond that bound (floor 5). A real subset of the dive "
        "disagrees with the fit -- either the laser moved partway through, "
        "or those frames' labels are wrong. Refusing to persist. " + _REMEDIES,
    ),
    "observation_geometry": (
        "CalibrationUnderdeterminedError",
        "laser observations span only 0.00 m of range (12 observations; "
        "gate 0.6 m). The lever arm, not the count, fixes the fitted "
        "direction: at this spread one pixel of dot-label noise moves every"
        " length by several percent. Shoot the target at two clearly "
        "different distances. Refusing to persist. " + _REMEDIES,
    ),
    "self_consistency_angle": (
        "CalibrationInconsistentError",
        "fitted laser ray reprojects at angle 90.00 deg to the input dot "
        "line (gate 3.0 deg); median offset 120.9px. The fit disagrees with"
        " its own observations — mixed dot populations (e.g. "
        "specular-reflection mislabels) or corrupt slate poses. Refusing to"
        " persist. " + _REMEDIES,
    ),
    "self_consistency_offset": (
        "CalibrationInconsistentError",
        "fitted laser ray reprojects with median offset 20.0px from the "
        "input dot line (gate 8.0px); angle 0.00 deg. Refusing to persist. "
        + _REMEDIES,
    ),
}


def run_case(name: str, monkeypatch=None):
    """`fit_laser` on case `name`, with the kernel's answer forced."""
    origin, axis, points, dots, mission_dots = CASES[name]

    def _kernel(_points):
        return np.array(origin[:2]), np.array(axis)

    if monkeypatch is None:
        saved = fit_module._calibrate_laser
        fit_module._calibrate_laser = _kernel
    else:
        monkeypatch.setattr(fit_module, "_calibrate_laser", _kernel)
    try:
        return fit_module.fit_laser(
            points,
            dots,
            K,
            mission_dots,
            too_few_type="InsufficientLaserPoints",
            too_few_reason=lambda n, m: f"{n} < {m}",
        )
    finally:
        if monkeypatch is None:
            fit_module._calibrate_laser = saved


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_stored_refusal_is_byte_identical(name, monkeypatch):
    result = run_case(name, monkeypatch)

    assert result.outcome == "refused"
    assert (result.refusal_type, result.refusal_reason) == EXPECTED[name]


def test_every_gate_branch_is_pinned():
    """Six messages: geometry, two self-consistency branches, baseline, and
    two describes-the-dive branches."""
    assert set(EXPECTED) == set(CASES)
