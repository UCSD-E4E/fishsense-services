"""Stage 14's processor activity: a fish length per capture.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
measure_fish_activity.py (`_measure_length`, and the non-finite drop). v1's
activity also chose the images, found or created species and fish, rebound
clusters, invalidated stale bindings and wrote the rows; in v2 those are the
orchestrator's and the store's, and this activity does only the geometry:
triangulate the laser dot for a depth, and measure head-to-tail across a
fronto-parallel plane at that depth (`laser_geometry`; camera matrix only, no
distortion, no sign flip -- the stage-14 sign investigation).

v1's parity rule, kept deliberately: the length is taken at the laser's depth
**whatever its sign**. v1's stage 14 guarded only on `isfinite`; the depth
stage's `depth_m > 0` gate was never applied here, and applying it now would
change which frames are measured and so the §6.2 parity numbers.

v2 changes:

* a non-finite length (v1: `dropped_nan`, nothing written) and a **zero**
  length (v1 wrote it; v2's measurements refuse it, CHECK length_m > 0) come
  back as **refusals** the orchestrator records, so the cohort stops offering
  the capture until one of its inputs changes (PLAN.md §9.16);
* the result names the algorithm, its version and the core wheel, which 0013
  requires of every server measurement.
"""

from __future__ import annotations

import math

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.measurement import (
    FishLength,
    MeasureFishInput,
    MeasureFishResult,
)
from fishsense_services_processor.laser_depth.activities import core_version
from fishsense_services_processor.laser_geometry import (
    compute_laser_point,
    measure_length_at_depth,
)

__all__ = ["ALGORITHM", "ALGORITHM_VERSION", "measure_fish"]

#: The single-depth, fronto-parallel projection of fishsense-lite@77e8f8e5
#: `laser_geometry`: the laser dot's Z, then norm(K^-1[h,1]Z - K^-1[t,1]Z).
ALGORITHM = "laser_depth_fronto_parallel"
#: Bump when the length a given set of inputs yields changes.
ALGORITHM_VERSION = "1"


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


@activity.defn(name="measure_fish")
async def measure_fish(payload: MeasureFishInput) -> MeasureFishResult:
    """Each capture's head-to-tail length at its laser dot's depth."""
    lengths: list[FishLength] = []
    for capture in payload.captures:
        try:
            point = compute_laser_point(
                capture.laser, payload.calibration, payload.camera_matrix
            )
        except ValueError as exc:
            raise ApplicationError(
                f"laser calibration {payload.calibration.laser_calibration_id}"
                f" of dive {payload.dive_id} is unusable: {exc}",
                type="UnusableLaserCalibration",
                non_retryable=True,
            ) from exc
        length_m = measure_length_at_depth(
            capture.head_tail, point.depth_m, payload.camera_matrix
        )
        if not math.isfinite(length_m):
            refusal = "non_finite_length"
        elif length_m <= 0.0:
            refusal = "zero_length"
        else:
            refusal = None
        if refusal is not None:
            activity.logger.warning(
                "dive_id=%s capture_id=%s: length=%s; refusing it (%s)",
                payload.dive_id,
                capture.capture_id,
                length_m,
                refusal,
            )
        lengths.append(
            FishLength(
                capture_id=capture.capture_id,
                species_label_id=capture.species_label_id,
                laser=capture.laser,
                head_tail=capture.head_tail,
                length_m=None if refusal else length_m,
                depth_m=_finite(point.depth_m),
                refusal=refusal,
            )
        )
        activity.heartbeat()

    activity.logger.info(
        "dive_id=%s measured=%d refused=%d",
        payload.dive_id,
        sum(1 for length in lengths if length.refusal is None),
        sum(1 for length in lengths if length.refusal is not None),
    )
    return MeasureFishResult(
        dive_id=payload.dive_id,
        algorithm=ALGORITHM,
        algorithm_version=ALGORITHM_VERSION,
        core_version=core_version(),
        captures=lengths,
    )
