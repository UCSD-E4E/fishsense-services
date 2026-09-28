"""Pin down the math convention of

Ported verbatim from fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/tests/test_compute_world_point_from_depth_convention.py.
fishsense_core.world_point.WorldPointHandler.compute_world_point_from_depth.

Stage 14 (not yet ported) lifts a 2D head/tail label to 3D using a depth
derived from a laser triangulation. The notebook flags that older
callers applied an external sign flip; the core method does not. These
tests verify the actual convention end-to-end so a future stage-14 port
can rely on `K^-1 · [x, y, 1] · depth` semantics with no external flip.

Pure-numpy, no rawpy / Temporal / httpx — runs in milliseconds.
"""

import numpy as np

from fishsense_core.world_point import WorldPointHandler


def _make_K(
    fx: float = 1000.0,
    fy: float = 1000.0,
    cx: float = 960.0,
    cy: float = 540.0,
) -> np.ndarray:
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def _project(K: np.ndarray, P: np.ndarray) -> np.ndarray:
    """Pinhole-project a 3D point to image coords (no distortion)."""
    homog = K @ P
    return homog[:2] / homog[2]


def _handler(K: np.ndarray) -> WorldPointHandler:
    return WorldPointHandler(np.linalg.inv(K))


def test_principal_point_lifts_to_z_axis_at_any_depth():
    """The principal point (cx, cy) is on the optical axis. Lifting it
    at any depth d should yield (0, 0, d)."""
    K = _make_K()
    handler = _handler(K)
    cx, cy = K[0, 2], K[1, 2]
    for depth in (0.5, 2.0, 10.0):
        result = handler.compute_world_point_from_depth(np.array([cx, cy]), depth)
        np.testing.assert_allclose(result, [0.0, 0.0, depth], atol=1e-6)


def test_round_trip_recovers_known_3d_point():
    """Project a known 3D point to image space, then lift back. Tight
    tolerance because both directions are pure linear algebra."""
    K = _make_K()
    handler = _handler(K)
    for P in (
        np.array([0.5, -0.3, 2.0]),
        np.array([-1.0, 0.7, 5.0]),
        np.array([0.2, 0.4, 1.5]),
    ):
        p_img = _project(K, P)
        recovered = handler.compute_world_point_from_depth(p_img, P[2])
        np.testing.assert_allclose(recovered, P, rtol=1e-6, atol=1e-6)


def test_depth_scales_world_point_linearly():
    """`K^-1 · [x, y, 1] · depth` is linear in depth — same image
    point, double the depth, double the world point."""
    handler = _handler(_make_K())
    image_point = np.array([1100.0, 600.0])
    base = handler.compute_world_point_from_depth(image_point, 1.0)
    doubled = handler.compute_world_point_from_depth(image_point, 2.0)
    np.testing.assert_allclose(doubled, 2 * base, rtol=1e-6, atol=1e-6)


def test_positive_depth_yields_positive_z():
    """The actual sign-flip concern. The notebook comment claims the
    core method matches the original Rust convention (no flip), so a
    positive depth must produce a world point with positive z. If this
    fails, stage-14 callers DO need an external flip and the notebook
    refactor is wrong."""
    handler = _handler(_make_K())
    result = handler.compute_world_point_from_depth(np.array([1100.0, 600.0]), 2.0)
    assert result[2] > 0, f"expected positive z for positive depth, got {result}"


def test_round_trip_with_realistic_olympus_intrinsics():
    """Sanity-check the convention at the K shape we actually use in
    the .ORF integration tests (~4000x3000 sensor, fx≈fy≈3000)."""
    K = _make_K(fx=3000.0, fy=3000.0, cx=2000.0, cy=1500.0)
    handler = _handler(K)
    # ~5cm right, 3cm down, 1.2m in front of camera.
    P = np.array([0.05, 0.03, 1.2])
    p_img = _project(K, P)
    recovered = handler.compute_world_point_from_depth(p_img, P[2])
    np.testing.assert_allclose(recovered, P, rtol=1e-6, atol=1e-6)
