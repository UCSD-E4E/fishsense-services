"""Unit tests for the two-line (specular reflection) detector.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_reflection_detection.py (the detector ports into the
processor as pipeline policy).

Prod dive 77 (2026-08-04): pool slate photos show the laser AND its specular
reflection ~45 px apart; labelers clicked the reflection on 34 of 79 frames.
The dots formed two coherent parallel lines — a population the RANSAC
validator cannot handle: the two-line split kills its confidence / trips the
MAX_OUTLIER_FRACTION refusal, so it silently skips the dive forever while
stage 13 consumes the poisoned mix. The detector names that failure mode
loudly instead.
"""

from __future__ import annotations

import numpy as np

from fishsense_core.laser import (
    fit_dive_line,
)
from fishsense_services_processor.laser_validation.reflection import (
    detect_reflection_split,
)


def _line_points(n, x0, y0, dx, dy, noise=0.8, seed=3):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 900, n)
    pts = np.stack([x0 + t * dx, y0 + t * dy], axis=1)
    return pts + rng.normal(0, noise, pts.shape)


def test_detects_parallel_reflection_split():
    primary = _line_points(30, 1900.0, 800.0, 0.3, 1.0)
    artifact = primary[:20] + np.array([45.0, 0.0])  # parallel, 45px away
    xy = np.vstack([primary, artifact])
    fit = fit_dive_line(xy)

    suspect = detect_reflection_split(xy, fit)

    assert suspect is not None
    assert suspect.n_secondary >= 15
    assert 30.0 < suspect.separation_px < 60.0
    assert suspect.angle_deg < 3.0


def test_clean_line_with_random_outliers_is_not_a_split():
    primary = _line_points(40, 1900.0, 800.0, 0.3, 1.0)
    rng = np.random.default_rng(11)
    scatter = rng.uniform([1000, 500], [3000, 2500], size=(6, 2))
    xy = np.vstack([primary, scatter])
    fit = fit_dive_line(xy)

    assert detect_reflection_split(xy, fit) is None


def test_small_second_cluster_is_not_a_split():
    primary = _line_points(40, 1900.0, 800.0, 0.3, 1.0)
    artifact = primary[:3] + np.array([45.0, 0.0])  # only 3 points
    xy = np.vstack([primary, artifact])
    fit = fit_dive_line(xy)

    assert detect_reflection_split(xy, fit) is None


def test_crossing_line_is_not_a_reflection():
    """A second line at a steep angle is some other pathology — the
    reflection signature is parallelism."""
    primary = _line_points(30, 1900.0, 800.0, 0.3, 1.0)
    crossing = _line_points(20, 1200.0, 1500.0, 1.0, 0.05, seed=5)
    xy = np.vstack([primary, crossing])
    fit = fit_dive_line(xy)

    assert detect_reflection_split(xy, fit) is None
