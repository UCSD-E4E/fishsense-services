"""Stage 14's processor activity: a fish length per capture.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
measure_fish_activity.py (`_measure_length`, and the non-finite drop). v1's
activity also chose the images, found or created species and fish, rebound
clusters, invalidated stale bindings and wrote the rows; in v2 those are the
orchestrator's and the store's, and this activity does only the geometry:
triangulate the laser dot for a depth, and measure head-to-tail across a
fronto-parallel plane at that depth (fishsense-core `world_point`; camera
matrix only, no distortion, no sign flip -- the stage-14 sign investigation).

v2 changes:

* a non-finite length (v1: `dropped_nan`, nothing written) and a **zero**
  length (v1 wrote it; v2's measurements refuse it, CHECK length_m > 0) come
  back as **refusals** the orchestrator records, so the cohort stops offering
  the capture until one of its inputs changes (PLAN.md §9.16);
* the result names the algorithm, its version and the core wheel, which 0013
  requires of every server measurement;
* **version 2 (2026-10-23, core #96) gives up v1's sign parity.** v1, and v2
  until then, took the length at the laser's depth *whatever its sign*
  (`isfinite` only), so a dot behind the camera measured as its positive
  twin, and a dot near the laser's vanishing point at a finite, positive,
  wrong depth. The owner chose fishsense-core's gate over that parity: the
  dot must pass `LaserPoint.is_measurable()`, or the capture is refused with
  the depth stage's reasons (`laser_dots.dot_refusal`:
  `non_finite_depth`, `non_positive_depth`, `low_parallax`), and core's
  `measure_length_at_depth` now raises on a depth that is not positive. A
  non-finite depth was `non_finite_length` under version 1. Where both
  versions measure, the length is the same (20,000 random dots: core matched
  `laser_geometry.py`, now gone, exactly). The cohort does not select on the
  version, so existing version-1 measurements stay current; only captures
  measured from now on are gated.
"""

from __future__ import annotations

import math

from fishsense_core.world_point import measure_length_at_depth
from temporalio import activity

from fishsense_services_contracts.measurement import (
    FishLength,
    MeasureFishInput,
    MeasureFishResult,
)
from fishsense_services_processor.laser_dots import (
    core_version,
    dot_refusal,
    laser_point,
)

__all__ = ["ALGORITHM", "ALGORITHM_VERSION", "measure_fish"]

#: The single-depth, fronto-parallel projection of fishsense-lite@77e8f8e5
#: `laser_geometry`: the laser dot's Z, then norm(K^-1[h,1]Z - K^-1[t,1]Z).
ALGORITHM = "laser_depth_fronto_parallel"
#: Bump when the length a given set of inputs yields changes. 2 (2026-10-23):
#: behind `LaserPoint.is_measurable()` (see the module docstring).
#: `measurement_work` does not select on it, so a capture measured under 1
#: is not measured again. That leaves no wrong length behind: production's
#: 22,398 measured laser dots were checked (2026-10-24, read-only) and none
#: is under 0.005 rad of parallax -- the least is 0.0133 rad, 2.7x the
#: threshold -- so every v1 length stands as the gate would have let it
#: (review of #361).
ALGORITHM_VERSION = "2"


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


@activity.defn(name="measure_fish")
async def measure_fish(payload: MeasureFishInput) -> MeasureFishResult:
    """Each capture's head-to-tail length at its laser dot's depth."""
    lengths: list[FishLength] = []
    for capture in payload.captures:
        point = laser_point(
            (capture.laser.x, capture.laser.y),
            payload.calibration.laser_position,
            payload.calibration.laser_axis,
            payload.camera_matrix,
            of=f"laser calibration {payload.calibration.laser_calibration_id}"
            f" of dive {payload.mission_id}",
        )
        refusal = dot_refusal(point)
        length_m = math.nan
        if refusal is None:
            ht = capture.head_tail
            length_m = measure_length_at_depth(
                (ht.head_x, ht.head_y),
                (ht.tail_x, ht.tail_y),
                point.depth_m,
                payload.camera_matrix,
            )
            if not math.isfinite(length_m):
                refusal = "non_finite_length"
            elif length_m <= 0.0:
                refusal = "zero_length"
        if refusal is not None:
            activity.logger.warning(
                "dive_id=%s capture_id=%s: depth=%s parallax_rad=%s length=%s; "
                "refusing it (%s)",
                payload.mission_id,
                capture.capture_id,
                point.depth_m,
                point.parallax_rad,
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
        payload.mission_id,
        sum(1 for length in lengths if length.refusal is None),
        sum(1 for length in lengths if length.refusal is not None),
    )
    return MeasureFishResult(
        mission_id=payload.mission_id,
        algorithm=ALGORITHM,
        algorithm_version=ALGORITHM_VERSION,
        core_version=core_version(),
        captures=lengths,
    )
