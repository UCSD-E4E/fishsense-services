"""End-to-end sign-consistency check for stage 14's measurement pipeline.

Ported verbatim from fishsense-lite@77e8f8e5
services/fishsense-data-processing-workflow-worker/tests/test_stage14_pipeline_sign_consistency.py.

The actual stage-14 risk is not whether `compute_world_point_from_depth`
is mathematically correct in isolation (covered by
`test_compute_world_point_from_depth_convention.py`), but whether it
agrees on sign convention with `compute_world_point_from_laser`. Stage
14 feeds `laser3d[2]` (z-component of the laser triangulation) as the
depth into `_from_depth`. If the two kernels disagreed on sign,
`laser3d[2]` would have the wrong sign on the way in and the head/tail
3D positions would be mirrored across the camera plane.

Crucially, fish length (`norm(head3d - tail3d)`) is sign-invariant — a
mirrored pipeline still produces the right length. So the canonical
test asserts on the absolute 3D positions, not just the length.

Pure-numpy synthetic geometry, no rawpy / Temporal / httpx — runs in
milliseconds.

**On the tolerance.** These assertions used `rtol=1e-4`, which sat inside the
triangulation's own noise band rather than outside it: the solve runs in
float32, and its error grows as the laser baseline shrinks (error ∝ Z²/(f·b),
the same conditioning the accuracy work measured). Sampled over 3098 random
geometries at a realistic baseline (≥8 cm; the rig's is ~10.4 cm) and 0.5–3 m
range, the relative depth error is p50 1.0e-5, p99 1.4e-4, max 3.2e-4 — so a
1e-4 bound fails a small fraction of geometries by luck. It was measured at
both fishsense-core 2.4.1 and 3.0.0 and is the same in each (2.4.1: p99
1.34e-4, max 2.36e-4), i.e. a property of the kernel's precision, not of a
version. `POSITION_RTOL` is set an order of magnitude above it — still 0.1%,
which is ~50× tighter than the pipeline's best-case ~1% accuracy, so it
remains far too tight for a genuine sign flip (which mirrors the point) to
slip through.
"""

import numpy as np

from fishsense_core.world_point import WorldPointHandler

# See the module docstring: above the kernel's measured float32 noise floor,
# far below anything a real convention error could hide in.
POSITION_RTOL = 1e-3
POSITION_ATOL = 1e-3


def _make_K(
    fx: float = 1000.0,
    fy: float = 1000.0,
    cx: float = 960.0,
    cy: float = 540.0,
) -> np.ndarray:
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def _project(K: np.ndarray, P: np.ndarray) -> np.ndarray:
    """Pinhole-project a camera-space 3D point to image coords."""
    homog = K @ P
    return homog[:2] / homog[2]


def _normalize(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def test_laser_triangulation_recovers_known_intersection_point():
    """When the camera ray and laser line intersect exactly,
    `compute_world_point_from_laser` must return the intersection
    point. This is the input to stage 14's depth handoff."""
    K = _make_K()
    handler = WorldPointHandler(np.linalg.inv(K))

    laser_origin = np.array([0.05, 0.0, 0.0])
    laser_axis = _normalize(np.array([0.0, 0.0, 1.0]))
    # Known 3D laser hit: 1m downrange along the laser line.
    P_laser = laser_origin + 1.0 * laser_axis

    laser_image_point = _project(K, P_laser)
    laser3d = handler.compute_world_point_from_laser(
        laser_origin, laser_axis, laser_image_point
    )

    np.testing.assert_allclose(laser3d, P_laser, rtol=POSITION_RTOL, atol=POSITION_ATOL)


def test_full_pipeline_recovers_head_tail_positions_and_length():
    """The canonical sign-consistency check.

    Lays out a synthetic scene: laser hit, head, and tail at known 3D
    positions all at the same depth. Runs stage 14's exact sequence
    (`_from_laser` -> use laser3d.z as depth -> `_from_depth` for head
    and tail) and asserts the recovered 3D positions match the ground
    truth. A sign disagreement between the two kernels would mirror
    head3d/tail3d across the camera plane (negative z) while still
    producing the correct length, so the position check is the one
    that catches it."""
    K = _make_K()
    handler = WorldPointHandler(np.linalg.inv(K))

    laser_origin = np.array([0.05, 0.0, 0.0])
    laser_axis = _normalize(np.array([0.0, 0.0, 1.0]))
    P_laser = laser_origin + 1.2 * laser_axis  # 1.2m downrange

    P_head = np.array([-0.10, 0.05, 1.2])
    P_tail = np.array([0.10, 0.05, 1.2])
    expected_length = np.linalg.norm(P_head - P_tail)

    laser_image = _project(K, P_laser)
    head_image = _project(K, P_head)
    tail_image = _project(K, P_tail)

    laser3d = handler.compute_world_point_from_laser(
        laser_origin, laser_axis, laser_image
    )
    head3d = handler.compute_world_point_from_depth(head_image, laser3d[2])
    tail3d = handler.compute_world_point_from_depth(tail_image, laser3d[2])

    # Length: passes even under sign flip (norm-invariant). Documents
    # the lower-bar guarantee.
    length = float(np.linalg.norm(head3d - tail3d))
    np.testing.assert_allclose(length, expected_length, rtol=POSITION_RTOL)

    # Absolute positions: this is the real check. A sign disagreement
    # between _from_laser and _from_depth would mirror these.
    np.testing.assert_allclose(laser3d, P_laser, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(head3d, P_head, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(tail3d, P_tail, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    assert head3d[2] > 0 and tail3d[2] > 0, (
        f"head/tail z must be positive (in front of camera); "
        f"got head={head3d}, tail={tail3d}"
    )


def test_pipeline_works_with_off_axis_laser():
    """Same property, but with a laser that's not parallel to the
    optical axis. Catches sign issues that only manifest off-axis."""
    K = _make_K()
    handler = WorldPointHandler(np.linalg.inv(K))

    laser_origin = np.array([0.04, -0.02, 0.0])
    laser_axis = _normalize(np.array([0.05, 0.03, 1.0]))
    t = 1.5
    P_laser = laser_origin + t * laser_axis

    P_head = np.array([P_laser[0] - 0.07, P_laser[1] + 0.02, P_laser[2]])
    P_tail = np.array([P_laser[0] + 0.07, P_laser[1] + 0.02, P_laser[2]])
    expected_length = np.linalg.norm(P_head - P_tail)

    laser3d = handler.compute_world_point_from_laser(
        laser_origin, laser_axis, _project(K, P_laser)
    )
    head3d = handler.compute_world_point_from_depth(_project(K, P_head), laser3d[2])
    tail3d = handler.compute_world_point_from_depth(_project(K, P_tail), laser3d[2])

    np.testing.assert_allclose(laser3d, P_laser, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(head3d, P_head, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(tail3d, P_tail, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(
        float(np.linalg.norm(head3d - tail3d)), expected_length, rtol=POSITION_RTOL
    )


def test_pipeline_works_with_realistic_olympus_intrinsics():
    """Sanity-check at the K shape used in our integration tests."""
    K = _make_K(fx=3000.0, fy=3000.0, cx=2000.0, cy=1500.0)
    handler = WorldPointHandler(np.linalg.inv(K))

    laser_origin = np.array([0.05, 0.0, 0.0])
    laser_axis = _normalize(np.array([0.0, 0.0, 1.0]))
    P_laser = laser_origin + 1.0 * laser_axis

    # Realistic ~25cm fish at ~1m range.
    P_head = np.array([-0.12, 0.03, 1.0])
    P_tail = np.array([0.13, 0.04, 1.0])

    laser3d = handler.compute_world_point_from_laser(
        laser_origin, laser_axis, _project(K, P_laser)
    )
    head3d = handler.compute_world_point_from_depth(_project(K, P_head), laser3d[2])
    tail3d = handler.compute_world_point_from_depth(_project(K, P_tail), laser3d[2])

    np.testing.assert_allclose(head3d, P_head, rtol=POSITION_RTOL, atol=POSITION_ATOL)
    np.testing.assert_allclose(tail3d, P_tail, rtol=POSITION_RTOL, atol=POSITION_ATOL)
