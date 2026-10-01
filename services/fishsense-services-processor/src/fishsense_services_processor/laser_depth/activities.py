"""The laser-depth activity: how far away each laser dot was.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
compute_laser_depths_activity.py. v1's activity read the dive's calibration and
labels through the API SDK, skipped the images already current, and wrote a
`LaserDepth` per image. In v2 the orchestrator does the reading, skipping and
writing (its cohort decides what is current); this activity keeps v1's
per-image rule:

* try the image's valid labels **in the order given** (v1: ascending label
  id) and take the first whose depth is finite and **> 0**. The gate is
  `depth_m > 0`, not finiteness: the kernel reports "these rays never meet"
  as the origin, and a dot on the wrong side of the principal point as a
  negative Z -- both finite, and a length computed at a negative depth is
  identical to its positive twin, so nothing downstream would notice;
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
  v1 retried it for the activity's whole hour.
"""

from __future__ import annotations

import importlib.metadata
import math

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserDepth,
    LaserDepthOutcome,
    LaserDepthRefusal,
)
from fishsense_services_processor.laser_geometry import compute_laser_point

__all__ = ["compute_laser_depths", "core_version"]


def core_version() -> str:
    """The fishsense-core wheel doing the geometry, as results record it."""
    return importlib.metadata.version("fishsense-core")


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
            try:
                point = compute_laser_point(
                    dot, payload.calibration, payload.camera_matrix
                )
            except ValueError as exc:
                # The kernel refuses a zero-length (or non-finite) axis. It is
                # the dive's calibration, not this image, that is broken.
                raise ApplicationError(
                    f"laser calibration {payload.calibration.laser_calibration_id}"
                    f" of dive {payload.dive_id} is unusable: {exc}",
                    type="UnusableLaserCalibration",
                    non_retryable=True,
                ) from exc
            if math.isfinite(point.depth_m) and point.depth_m > 0.0:
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
                "dive_id=%s capture_id=%s laser_label_id=%s: depth=%s; "
                "trying the capture's next valid label",
                payload.dive_id,
                capture.capture_id,
                dot.laser_label_id,
                point.depth_m,
            )
            refusals.append(
                LaserDepthRefusal(
                    laser_label_id=dot.laser_label_id,
                    x=dot.x,
                    y=dot.y,
                    reason=(
                        "non_positive_depth"
                        if math.isfinite(point.depth_m)
                        else "non_finite_depth"
                    ),
                    depth_m=_finite(point.depth_m),
                )
            )
        if depth is None:
            refused += 1
            activity.logger.warning(
                "dive_id=%s capture_id=%s: none of its %d valid laser label(s) "
                "intersect laser_calibration_id=%s in front of the camera; "
                "refusing it",
                payload.dive_id,
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
        payload.dive_id,
        computed,
        refused,
    )
    return ComputeLaserDepthsResult(
        dive_id=payload.dive_id, core_version=core_version(), captures=outcomes
    )
