"""Automatic lengths: stage 14's geometry on the automatic inputs.

New in v2. The geometry is `laser_geometry` (the stage-14 kernel: the dot's
depth by closest approach of the camera ray and the laser, then head-to-tail
across the fronto-parallel plane at that depth), as cscw-fishsense2027@96a8da07
e2e_measurement/score.py `depth`/`length` compute it. One change from stage
14, which keeps v1's sign parity: **the depth is gated** (> 0). A dot that
triangulates behind the camera is no observation (laser_geometry's own
docstring), and nothing here has a parity to keep.
"""

from __future__ import annotations

import math
from importlib.metadata import version
from types import SimpleNamespace

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_MEASUREMENT_ALGORITHM,
    AUTOMATIC_MEASUREMENT_VERSION,
    AutomaticLength,
    MeasureAutomaticInput,
    MeasureAutomaticResult,
)
from fishsense_services_processor.laser_geometry import (
    compute_laser_point,
    measure_length_at_depth,
)

__all__ = ["measure_automatic"]


@activity.defn(name="measure_automatic")
async def measure_automatic(payload: MeasureAutomaticInput) -> MeasureAutomaticResult:
    """Each automatic fish's length at its automatic dot's depth."""
    calibration = SimpleNamespace(
        laser_position=payload.laser_position, laser_axis=payload.laser_axis
    )
    lengths = []
    for c in payload.captures:
        try:
            point = compute_laser_point(
                SimpleNamespace(x=c.laser_x, y=c.laser_y),
                calibration,
                payload.camera_matrix,
            )
        except ValueError as exc:
            raise ApplicationError(
                f"dive {payload.dive_id}'s calibration is unusable: {exc}",
                type="UnusableLaserCalibration",
                non_retryable=True,
            ) from exc
        depth = point.depth_m if math.isfinite(point.depth_m) else None
        length, refusal = None, None
        if depth is None or depth <= 0:
            refusal = "non_positive_depth"
        else:
            length = measure_length_at_depth(c, depth, payload.camera_matrix)
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
        payload.dive_id,
        sum(1 for x in lengths if x.refusal is None),
        sum(1 for x in lengths if x.refusal is not None),
    )
    return MeasureAutomaticResult(
        dive_id=payload.dive_id,
        algorithm=AUTOMATIC_MEASUREMENT_ALGORITHM,
        algorithm_version=AUTOMATIC_MEASUREMENT_VERSION,
        core_version=version("fishsense-core"),
        lengths=lengths,
    )
