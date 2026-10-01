"""The dive line is fish-dominated, so it must not judge slate dots at 3 sigma.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_coarse_calibration_frame_supersede.py: it pins
fishsense-core 4.1.0's coarse rule, which the v2 validator relies on.

Measured on prod 2026-09-13, against each dive's own line fitted through its
live measurement-frame dots:

    dive 347   319 fish dots, median 1.25 px off, max 3.1
               slate dots: 0.34, 3.44-8.48 (eleven frames, visually confirmed
               genuine), then 45.57, 50.59, 83.99, 130.18 (four mislabels)
    dive 349   125 fish dots, median 0.94 px off, max 2.9
               slate dots: 2.87-5.98, all twelve genuine

The 3 sigma test with a 1 px MAD floor lands at 3.56 px (347) and 3.00 px
(349), so it superseded eleven of 347's thirteen genuine slate dots and ten
of 349's twelve -- leaving each dive one or two usable observations, which is
exactly how 347 came to be calibrated from a single frame and 349 from two
dots 26 cm apart in range.

Why genuine slate dots sit a few px off a line their own dive's fish frames
define to ~1 px: the slate burst is a few seconds long and the fish frames
are minutes away, so a small in-plane rotation of the laser inside its mount
moves the burst coherently; and the burst is a dozen dots against hundreds,
so it cannot pull the fit toward itself.

The asymmetry is what sets the tolerance. Superseding a genuine slate dot
removes the dive's only route to a calibration and cannot be undone by
relabelling (`get_laser_label_by_label_studio_id` filters superseded rows),
while a 10 px error in one calibration dot is diluted by the others and
caught downstream by the four calibration gates. A 45+ px dot is a different
population -- a specular reflection or another object -- and must still go.

So calibration frames are judged coarsely and in absolute pixels, with the
bound between the two measured populations.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_core.laser import (
    COARSE_CALIBRATION_TOLERANCE_PX,
    fit_dive_line,
    flag_outliers,
)


def _dive(n_fish: int = 300, slate_offsets=(), fish_offsets=()) -> tuple:
    """A fish-dominated dive line plus slate dots at given offsets (px).

    Returns (xy, calibration_mask) with the slate dots last.
    """
    rng = np.random.default_rng(7)
    xs = np.linspace(50.0, 1500.0, n_fish)
    ys = 0.4 * xs + 100.0 + rng.normal(0.0, 1.0, size=n_fish)
    normal = np.array([-0.4, 1.0]) / np.hypot(0.4, 1.0)
    xy = np.column_stack([xs, ys])
    for i, off in enumerate(fish_offsets):
        xy[i] = xy[i] + off * normal
    slate = []
    for i, off in enumerate(slate_offsets):
        x = 700.0 + 3.0 * i
        base = np.array([x, 0.4 * x + 100.0])
        slate.append(base + off * normal)
    if slate:
        xy = np.vstack([xy, np.array(slate)])
    mask = np.zeros(xy.shape[0], dtype=bool)
    mask[n_fish:] = True
    return xy, mask


def test_genuine_slate_dots_a_few_px_off_the_fish_line_survive():
    """Dive 347's shape: eleven slate dots at 3.4-8.5 px, all real."""
    xy, mask = _dive(slate_offsets=(3.4, 3.5, 3.7, 3.8, 3.8, 4.9, 5.1, 8.5))
    fit = fit_dive_line(xy)
    flagged = flag_outliers(xy, fit, calibration_mask=mask)
    assert not flagged[mask].any()


def test_a_wild_slate_mislabel_is_still_superseded():
    """The four 347 rejected: 45.6, 50.6, 84.0, 130.2 px."""
    xy, mask = _dive(slate_offsets=(3.4, 4.9, 45.6, 50.6, 84.0, 130.2))
    fit = fit_dive_line(xy)
    flagged = flag_outliers(xy, fit, calibration_mask=mask)
    assert list(flagged[mask]) == [False, False, True, True, True, True]


def test_measurement_frames_are_still_judged_at_three_sigma():
    """The coarse tolerance applies to calibration frames only. A fish dot
    5 px off a 1 px line is still a mislabel and still goes."""
    xy, mask = _dive(slate_offsets=(4.0,), fish_offsets=(5.0, 40.0))
    fit = fit_dive_line(xy)
    flagged = flag_outliers(xy, fit, calibration_mask=mask)
    assert flagged[0] and flagged[1]
    assert not flagged[mask].any()


def test_without_a_mask_every_dot_is_judged_at_three_sigma():
    """The old behaviour, unchanged, so a caller that knows nothing about
    calibration frames cannot silently get the loose rule."""
    xy, mask = _dive(slate_offsets=(4.0, 8.5))
    fit = fit_dive_line(xy)
    assert flag_outliers(xy, fit)[mask].all()


def test_an_all_calibration_dive_still_rejects_its_wild_dots():
    """A slate-only calibration dive: the line is slate-dominated, every
    frame is a calibration frame, and the coarse rule still has to work."""
    rng = np.random.default_rng(3)
    xs = np.linspace(1900.0, 2000.0, 12)
    ys = 3.0 * xs - 4200.0 + rng.normal(0.0, 1.0, size=12)
    xy = np.column_stack([xs, ys])
    normal = np.array([-3.0, 1.0]) / np.hypot(3.0, 1.0)
    xy[-1] = xy[-1] + 60.0 * normal
    mask = np.ones(12, dtype=bool)
    fit = fit_dive_line(xy)
    flagged = flag_outliers(xy, fit, calibration_mask=mask)
    assert flagged[-1]
    assert not flagged[:-1].any()


def test_the_tolerance_sits_between_the_measured_populations():
    """Genuine slate dots reach 8.48 px (dive 347); the nearest mislabel is
    45.57. Tightening past 8.48 re-breaks 347 and 349; loosening past 45.57
    re-admits a reflection into a calibration."""
    assert 8.48 < COARSE_CALIBRATION_TOLERANCE_PX < 45.57


@pytest.mark.parametrize("offset", [0.0, 8.4, 18.0])
def test_a_calibration_dot_within_tolerance_survives(offset):
    """Offsets are nominal: the fitted line differs from the generating one by
    the noise, so a dot asked for at 19.9 px measures 20.03 and is correctly
    flagged. 18.0 leaves room for that without blunting the bound."""
    xy, mask = _dive(slate_offsets=(offset,))
    fit = fit_dive_line(xy)
    assert not flag_outliers(xy, fit, calibration_mask=mask)[mask].any()
