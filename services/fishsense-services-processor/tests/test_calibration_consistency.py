"""Unit tests for the stage-13 self-consistency gate (pure numpy, no cv2).

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_consistency.py.

The gate exists because of prod dive 77 (2026-08-04): labelers clicked the
laser's specular reflection on ~half the pool slate frames, the laser dots
formed two parallel lines, and the 3D fit split the difference — 14 deg off
the dive's own dot line, with a fleet-outlier origin. The bad calibration was
persisted, borrowed by a fish-model dive, and produced +31..+137% length
errors. A fit that cannot reproject onto the very dots it was computed from
must never persist. Fleet baseline: every good calibration reprojects onto
its own dots at <=1.6 deg / <=4 px median; 77-bad was 14 deg / 17 px.
"""

from __future__ import annotations

import numpy as np
import pytest

from fishsense_services_processor.calibration.consistency import (
    CalibrationInconsistentError,
    check_fit_self_consistency,
)

K = np.array(
    [
        [2840.0, 0.0, 2000.0],
        [0.0, 2860.0, 1450.0],
        [0.0, 0.0, 1.0],
    ]
)
ORIGIN = np.array([-0.031, -0.099, 0.0])
AXIS = np.array([0.01, 0.03, 0.999])
AXIS = AXIS / np.linalg.norm(AXIS)


def _project(p3: np.ndarray) -> tuple[float, float]:
    q = K @ p3
    return float(q[0] / q[2]), float(q[1] / q[2])


def _dots_on_ray(depths, noise_px: float = 0.0, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    pts = []
    for z in depths:
        t = (z - ORIGIN[2]) / AXIS[2]
        x, y = _project(ORIGIN + t * AXIS)
        pts.append((x + rng.normal(0, noise_px), y + rng.normal(0, noise_px)))
    return np.array(pts)


def test_consistent_fit_passes():
    dots = _dots_on_ray(np.linspace(0.4, 2.5, 20), noise_px=1.0)
    # Must not raise.
    check_fit_self_consistency(ORIGIN, AXIS, K, dots)


def test_rotated_axis_is_rejected():
    """A 10-deg axis error (the dive-77 class of failure) must be rejected."""
    dots = _dots_on_ray(np.linspace(0.4, 2.5, 20), noise_px=1.0)
    bad_axis = np.array([np.sin(np.radians(10.0)), 0.03, 0.99])
    bad_axis = bad_axis / np.linalg.norm(bad_axis)
    with pytest.raises(CalibrationInconsistentError, match="angle"):
        check_fit_self_consistency(ORIGIN, bad_axis, K, dots)


def test_two_line_mixture_is_rejected():
    """Dots split across two parallel lines (reflection artifact): the fit
    projects between them, so the median offset blows the gate."""
    line_a = _dots_on_ray(np.linspace(0.4, 2.5, 12), noise_px=1.0)
    line_b = line_a + np.array([45.0, 0.0])  # parallel artifact line
    dots = np.vstack([line_a, line_b])
    shifted_origin = ORIGIN + np.array([0.008, 0.0, 0.0])  # fit splits the gap
    with pytest.raises(CalibrationInconsistentError, match="offset"):
        check_fit_self_consistency(shifted_origin, AXIS, K, dots)


def test_small_span_skips_gate():
    """Two nearly-coincident dots can't determine a line direction — the gate
    must skip (not raise) rather than reject on meaningless geometry."""
    dots = _dots_on_ray([1.0, 1.005])
    bad_axis = np.array([np.sin(np.radians(10.0)), 0.03, 0.99])
    bad_axis = bad_axis / np.linalg.norm(bad_axis)
    # Must not raise despite the bad axis: span too small to judge.
    check_fit_self_consistency(ORIGIN, bad_axis, K, dots)


def test_offset_fit_is_rejected():
    """Right direction, wrong origin (parallel-shifted projection)."""
    dots = _dots_on_ray(np.linspace(0.4, 2.5, 20), noise_px=1.0)
    shifted = ORIGIN + np.array([0.012, 0.0, 0.0])  # ~30 px shift at ~1 m
    with pytest.raises(CalibrationInconsistentError, match="offset"):
        check_fit_self_consistency(shifted, AXIS, K, dots)
