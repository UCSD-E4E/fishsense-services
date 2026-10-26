"""Automatic lengths: stage 14's geometry on the automatic inputs.

New in v2. The geometry is fishsense-core `world_point` (the stage-14 kernel:
the dot's depth by closest approach of the camera ray and the laser, then
head-to-tail across the fronto-parallel plane at that depth), as
cscw-fishsense2027@96a8da07 e2e_measurement/score.py `depth`/`length` compute
it.

**The dot is gated on `LaserPoint.is_measurable()`**, as stage 14's now is.
Version 1 gated on a finite depth > 0, from the start: a dot that
triangulates behind the camera is no observation. Version 2 (2026-10-23, core
#96; `AUTOMATIC_MEASUREMENT_VERSION`) also refuses a dot near the laser's
vanishing point, whose finite, positive depth is untrustworthy, as
`low_parallax`. A depth that is not finite keeps version 1's name,
`non_positive_depth` (the stage's CHECK, migration 0036, has no other).
"""

from __future__ import annotations

import math
from importlib.metadata import version

from fishsense_core.world_point import measure_length_at_depth
from temporalio import activity

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_MEASUREMENT_ALGORITHM,
    AUTOMATIC_MEASUREMENT_VERSION,
    AutomaticLength,
    MeasureAutomaticInput,
    MeasureAutomaticResult,
)
from fishsense_services_processor.laser_dots import dot_refusal, laser_point

__all__ = ["measure_automatic"]


@activity.defn(name="measure_automatic")
async def measure_automatic(payload: MeasureAutomaticInput) -> MeasureAutomaticResult:
    """Each automatic fish's length at its automatic dot's depth."""
    lengths = []
    for c in payload.captures:
        point = laser_point(
            (c.laser_x, c.laser_y),
            payload.laser_position,
            payload.laser_axis,
            payload.camera_matrix,
            of=f"dive {payload.mission_id}'s automatic calibration",
        )
        depth = point.depth_m if math.isfinite(point.depth_m) else None
        # The dot's own reason, `non_finite_depth` included (review of #361:
        # it was stored as `non_positive_depth`, misnaming a finite, positive
        # depth with a non-finite range; 0071's CHECK names it).
        length, refusal = None, dot_refusal(point)
        if refusal is None:
            length = measure_length_at_depth(
                (c.head_x, c.head_y),
                (c.tail_x, c.tail_y),
                point.depth_m,
                payload.camera_matrix,
            )
            if not math.isfinite(length):
                length, refusal = None, "non_finite_length"
            elif length <= 0:
                length, refusal = None, "zero_length"
        lengths.append(
            AutomaticLength(
                capture_id=c.capture_id,
                automatic_head_tail_prediction_id=c.automatic_head_tail_prediction_id,
                length_m=length,
                depth_m=depth,
                refusal=refusal,
            )
        )
    activity.logger.info(
        "dive=%s automatic lengths=%d refused=%d",
        payload.mission_id,
        sum(1 for x in lengths if x.refusal is None),
        sum(1 for x in lengths if x.refusal is not None),
    )
    return MeasureAutomaticResult(
        mission_id=payload.mission_id,
        algorithm=AUTOMATIC_MEASUREMENT_ALGORITHM,
        algorithm_version=AUTOMATIC_MEASUREMENT_VERSION,
        core_version=version("fishsense-core"),
        lengths=lengths,
    )
