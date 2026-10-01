"""Trimming outliers before the laser line fit.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_robust_laser_fit.py.

`calibrate_laser` is plain least squares with no outlier rejection, so a single
badly-placed observation drags the whole line. That is not hypothetical: prod
dive 77 shipped a calibration whose lengths reached +137% because labelers
clicked the laser's specular reflection on about half the frames, and dive 103
carries a 6.25 cm baseline from **17 otherwise clean observations** — the shape
that neither a higher `MIN_LASER_POINTS` nor the baseline bound explains.

The damage is amplified by what the fit is asked for. The reported origin is
where the line crosses z=0, typically a metre or more behind the observations,
so a small angular error from one bad point is levered into a large error in
the baseline — which is exactly the quantity nothing downstream can see.

These tests are built on synthetic geometry with a known answer, because there
is no ground-truth set of real calibrations to validate against. That is a real
limit: they pin that trimming recovers the clean line when the contamination is
the shape we have seen, not that it helps on every real dive.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration.robust_fit import (
    MAX_TRIM_FRACTION,
    trim_outlying_observations,
)

#: A laser 10.4 cm off the camera centre, pointing essentially down the optical
#: axis — the fleet's real geometry.
ORIGIN = np.array([0.0624, 0.0832, 0.0])
AXIS = np.array([0.004, -0.006, 1.0]) / np.linalg.norm([0.004, -0.006, 1.0])


def _clean_points(n: int = 16, *, noise: float = 0.0, seed: int = 0):
    """Observations along the true laser ray, spread over a real depth range."""
    rng = np.random.default_rng(seed)
    depths = np.linspace(0.5, 3.0, n)
    points = ORIGIN[None, :] + (depths / AXIS[2])[:, None] * AXIS[None, :]
    if noise:
        points = points + rng.normal(0.0, noise, points.shape)
    return points


def test_clean_observations_are_all_kept():
    """Trimming must not fire on good data, or it silently discards signal."""
    points = _clean_points()

    kept = trim_outlying_observations(points)

    assert len(kept) == len(points)


def test_realistic_noise_is_kept():
    """Sub-centimetre scatter is ordinary and must not be mistaken for outliers."""
    points = _clean_points(noise=0.004)

    kept = trim_outlying_observations(points)

    assert len(kept) == len(points)


def test_a_single_gross_outlier_is_dropped():
    """One bad dot is the dive-103 shape: many clean points, one wrecking them."""
    points = _clean_points(noise=0.002)
    points[7] = points[7] + np.array([0.35, -0.25, 0.0])

    kept = trim_outlying_observations(points)

    assert len(kept) == len(points) - 1
    # The survivor set must be the clean ones, not merely one fewer.
    assert not np.any(np.all(np.isclose(kept, points[7]), axis=1))


def test_trimming_recovers_the_clean_geometry():
    """The property that matters: the fit after trimming is the fit without
    the outlier, not just a different fit."""
    clean = _clean_points(noise=0.002)
    contaminated = clean.copy()
    contaminated[3] = contaminated[3] + np.array([0.4, 0.3, 0.0])

    kept = trim_outlying_observations(contaminated)

    assert len(kept) == len(clean) - 1
    for point in kept:
        assert np.any(np.all(np.isclose(clean, point), axis=1))


def test_several_scattered_outliers_are_dropped():
    """Independently misplaced dots — the common case — are all removed.

    Displacements are fixed rather than sampled: a random draw occasionally
    lands a "outlier" close enough to the ray to be legitimately kept, which
    makes the test flaky about the threshold rather than about the property.
    """
    points = _clean_points(n=20, noise=0.002)
    points[2] = points[2] + np.array([0.30, -0.20, 0.0])
    points[9] = points[9] + np.array([-0.25, 0.35, 0.0])
    points[15] = points[15] + np.array([0.40, 0.15, 0.0])

    kept = trim_outlying_observations(points)

    assert len(kept) == 17


def test_a_parallel_reflection_cluster_is_not_trimmed():
    """The dive-77 shape, and the limit of this approach — pinned deliberately.

    When a contiguous subset is displaced by a *constant* offset it forms a
    second line parallel to the true one. The total-least-squares fit then
    splits the difference between the two, so every observation carries a
    similar moderate residual and none of them looks like an outlier. No
    residual-based trim can separate them, however the threshold is tuned.

    That failure is already covered, by `check_fit_self_consistency`: two
    parallel dot lines make the fitted ray reproject at an angle to the dots it
    came from, which is exactly what that gate measures (dive 77 read 14 deg
    against a fleet baseline of <=1.6 deg).

    So the two guards are complementary, not redundant — trimming handles
    isolated bad dots, self-consistency handles systematic contamination. This
    test exists so nobody later "fixes" the trimmer to chase this case and
    starts discarding good observations in the process.
    """
    points = _clean_points(n=20, noise=0.002)
    points[:5] = points[:5] + np.array([0.18, 0.0, 0.0])

    kept = trim_outlying_observations(points)

    assert len(kept) == 20


def test_it_refuses_to_trim_more_than_the_cap():
    """A cap, so a half-contaminated set cannot be silently 'cleaned'.

    If most observations disagree there is no majority to trust, and quietly
    fitting the larger half would produce a confident answer from data we have
    no reason to believe. Better to keep everything and let the baseline gate
    refuse the result.
    """
    points = _clean_points(n=20, noise=0.002)
    # Half the set displaced — no defensible majority.
    points[:10] = points[:10] + np.array([0.3, 0.25, 0.0])

    kept = trim_outlying_observations(points)

    assert len(kept) >= len(points) * (1 - MAX_TRIM_FRACTION)


def test_too_few_observations_are_returned_untouched():
    """At the `MIN_LASER_POINTS` floor there is no redundancy to trim with.

    Two points always fit a line exactly, so every residual is zero and any
    trim rule is reading noise. Returning them unchanged keeps this from
    turning a thin-but-valid calibration into no calibration at all.
    """
    points = _clean_points(n=2)

    kept = trim_outlying_observations(points)

    assert len(kept) == 2


@pytest.mark.parametrize("n", [0, 1])
def test_degenerate_inputs_pass_through(n):
    points = _clean_points(n=max(n, 1))[:n]

    kept = trim_outlying_observations(points)

    assert len(kept) == n


def test_input_is_not_mutated():
    points = _clean_points(noise=0.002)
    points[5] = points[5] + np.array([0.4, 0.0, 0.0])
    before = points.copy()

    trim_outlying_observations(points)

    assert np.array_equal(points, before)
