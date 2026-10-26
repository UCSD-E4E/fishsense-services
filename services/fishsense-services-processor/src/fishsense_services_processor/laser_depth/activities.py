"""The laser-depth activity: how far away each laser dot was.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
compute_laser_depths_activity.py. v1's activity read the dive's calibration and
labels through the API SDK, skipped the images already current, and wrote a
`LaserDepth` per image. In v2 the orchestrator does the reading, skipping and
writing (its cohort decides what is current); this activity keeps v1's
per-image rule:

* try the image's valid labels **in the order given** (v1: ascending label
  id) and take the first that gives a usable depth;
* a later label is tried when an earlier one is degenerate: dropping the
  image would leave the image-keyed cohort offering the dive forever (the
  dive-279 wedge, from the other side);
* the residual is recorded, never gated on (it is blind along the laser's
  epipolar line).

v2 changes:

* **every label that gave no depth comes back as a refusal**, naming what was
  tried. v1 counted `skipped_invalid_geometry` and wrote nothing, so the dive
  stayed in its cohort forever (dive 32 blocked 49 dives for 23 hours, PLAN.md
  §4.5/§9.16); the orchestrator now records the refusal and the cohort skips
  it until the label or the calibration changes;
* a directionless laser axis fails the activity **non-retryably**: it is a
  property of the dive's calibration, so every image fails the same way, and
  v1 retried it for the activity's whole hour;
* **the gate is fishsense-core's `LaserPoint.is_measurable()`** (2026-10-23,
  core #96; contract v21), where v1 and v2 until then asked for a finite
  depth `> 0`. The triangulation answers every pixel: rays that never meet
  come back at the origin, a dot on the wrong side of the principal point
  behind the camera, and -- the case `depth_m > 0` let through -- a dot near
  the laser's vanishing point, where the two rays are nearly parallel, at a
  finite, positive, wrong depth. `parallax_rad` catches that last one
  (`low_parallax`). The projection itself moved to fishsense-core with it
  (`world_point.compute_laser_point`, ported from this repo's
  `laser_geometry.py`, now gone); on 20,000 random dots the two agreed
  exactly.
"""

from __future__ import annotations

import math

from temporalio import activity

from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserDepth,
    LaserDepthOutcome,
    LaserDepthRefusal,
)
from fishsense_services_processor.laser_dots import (
    core_version,
    dot_refusal,
    laser_point,
)

__all__ = ["compute_laser_depths"]


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


@activity.defn(name="compute_laser_depths")
async def compute_laser_depths(
    payload: ComputeLaserDepthsInput,
) -> ComputeLaserDepthsResult:
    """The distance to each capture's laser dot, or why there is none."""
    outcomes: list[LaserDepthOutcome] = []
    computed = refused = 0
    for capture in payload.captures:
        depth: LaserDepth | None = None
        refusals: list[LaserDepthRefusal] = []
        for dot in capture.laser_labels:
            point = laser_point(
                (dot.x, dot.y),
                payload.calibration.laser_position,
                payload.calibration.laser_axis,
                payload.camera_matrix,
                of=f"laser calibration {payload.calibration.laser_calibration_id}"
                f" of dive {payload.mission_id}",
            )
            reason = dot_refusal(point)
            if reason is None:
                depth = LaserDepth(
                    laser_label_id=dot.laser_label_id,
                    x=dot.x,
                    y=dot.y,
                    depth_m=point.depth_m,
                    range_m=point.range_m,
                    # Recorded, not gated on: it cannot see error along the
                    # laser's epipolar line, and being metric it means
                    # different things at 0.9 m and 2.5 m.
                    residual_m=_finite(point.residual_m),
                )
                break
            activity.logger.debug(
                "dive_id=%s capture_id=%s laser_label_id=%s: depth=%s "
                "parallax_rad=%s (%s); trying the capture's next valid label",
                payload.mission_id,
                capture.capture_id,
                dot.laser_label_id,
                point.depth_m,
                point.parallax_rad,
                reason,
            )
            refusals.append(
                LaserDepthRefusal(
                    laser_label_id=dot.laser_label_id,
                    x=dot.x,
                    y=dot.y,
                    reason=reason,
                    # What the solve said; never a depth `laser_depths` would
                    # take, or it would have been one. A broken solve's finite
                    # depth is not recorded: it carries nothing.
                    depth_m=(None if reason == "non_finite_depth" else point.depth_m),
                )
            )
        if depth is None:
            refused += 1
            activity.logger.warning(
                "dive_id=%s capture_id=%s: none of its %d valid laser label(s) "
                "gives a measurable depth under laser_calibration_id=%s; "
                "refusing it",
                payload.mission_id,
                capture.capture_id,
                len(capture.laser_labels),
                payload.calibration.laser_calibration_id,
            )
        else:
            computed += 1
        outcomes.append(
            LaserDepthOutcome(
                capture_id=capture.capture_id, depth=depth, refusals=refusals
            )
        )
        activity.heartbeat()

    activity.logger.info(
        "dive_id=%s laser depths: computed=%d refused=%d",
        payload.mission_id,
        computed,
        refused,
    )
    return ComputeLaserDepthsResult(
        mission_id=payload.mission_id, core_version=core_version(), captures=outcomes
    )
