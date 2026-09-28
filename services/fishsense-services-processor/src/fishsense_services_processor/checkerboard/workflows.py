"""The checkerboard workflows on the processor: calibrate, and verify.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/workflows/
(perform_checkerboard_calibration_workflow.py,
verify_checkerboard_lattice_workflow.py).

**Calibration** has two phases because the two halves have different costs:
finding the board is per-frame and holds image bytes, while the fit is a few
milliseconds of arithmetic over a list of 3-D points. The fit is still an
activity, not workflow code: `calibrate_laser` is a Rust kernel and the gates
are numpy, neither of which may run in a deterministic, replayable workflow.

**Verification** has one phase: nothing is fitted, because the stage answers
a question about the detector. `sample_limit` is a plain head-of-list take,
NOT a random sample -- a stable subset keeps a re-run's verdicts comparable.
Applied here rather than by the parent's resolver: the parent has already
staged the dive's raw frames by now, and trimming there would mean a later
run wanting a different sample re-stages from the NAS.

Both fan out with `asyncio.gather` and run on the **per-image** role (v1's CPU
queue): each frame is a rawpy decode peaking at 1-3 GB, which is what the
role's concurrency cap of 2 exists for. v1 once ran the renders sequentially,
reasoning the memory peak made parallel dispatch pointless; the worker's cap
already bounds that, and the sequential loop serialised the fan-out under one
execution timeout.

v2 changes: the calibration child returns the fit's `LaserCalibrationResult`
for the orchestrator to persist (v1: the persisted row id), and frames and
renders are `ObjectRef`s the orchestrator issued.
"""

import asyncio
from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.slate_calibration import (
        CheckerboardLatticeRender,
        CheckerboardObservation,
        LaserCalibrationResult,
        PerformCheckerboardCalibrationInput,
        VerifyCheckerboardLatticeInput,
    )
    from fishsense_services_processor.checkerboard.activities import (
        DetectCheckerboardLaserPointInput,
        FitCheckerboardExtrinsicsInput,
        RenderCheckerboardLatticeInput,
    )

__all__ = [
    "PerformCheckerboardCalibrationWorkflow",
    "VerifyCheckerboardLatticeWorkflow",
]


@workflow.defn
class PerformCheckerboardCalibrationWorkflow:
    # pylint: disable=too-few-public-methods
    """Find the board in every frame, then fit the dive's laser from them."""

    @workflow.run
    async def run(
        self, payload: PerformCheckerboardCalibrationInput
    ) -> LaserCalibrationResult:
        workflow.logger.info(
            "checkerboard calibration dive_id=%s frames=%d board=%dx%d "
            "pitch=%.5f/%.5fm",
            payload.dive_id,
            len(payload.images),
            payload.target.rows,
            payload.target.cols,
            payload.target.pitch_x_m,
            payload.target.pitch_y_m,
        )

        observations = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "detect_checkerboard_laser_point",
                    DetectCheckerboardLaserPointInput(
                        capture_id=image.capture_id,
                        raw=image.raw,
                        laser_x=image.laser_x,
                        laser_y=image.laser_y,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                        target=payload.target,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    result_type=CheckerboardObservation,
                )
                for image in payload.images
            ]
        )

        return await workflow.execute_activity(
            "fit_checkerboard_laser_extrinsics",
            FitCheckerboardExtrinsicsInput(
                dive_id=payload.dive_id,
                camera_matrix=payload.camera_matrix,
                observations=list(observations),
                dive_dots=payload.dive_dots,
            ),
            start_to_close_timeout=timedelta(minutes=10),
            result_type=LaserCalibrationResult,
        )


@workflow.defn
class VerifyCheckerboardLatticeWorkflow:
    # pylint: disable=too-few-public-methods
    """Render each sampled frame's lattice and return what was drawn."""

    @workflow.run
    async def run(
        self, payload: VerifyCheckerboardLatticeInput
    ) -> List[CheckerboardLatticeRender]:
        images = payload.images
        if payload.sample_limit is not None:
            images = images[: payload.sample_limit]

        workflow.logger.info(
            "lattice verification dive_id=%s frames=%d of %d board=%dx%d",
            payload.dive_id,
            len(images),
            len(payload.images),
            payload.target.rows,
            payload.target.cols,
        )

        renders = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "render_checkerboard_lattice",
                    RenderCheckerboardLatticeInput(
                        capture_id=image.capture_id,
                        raw=image.raw,
                        render=image.render,
                        laser_x=image.laser_x,
                        laser_y=image.laser_y,
                        camera_matrix=payload.camera_matrix,
                        distortion_coefficients=payload.distortion_coefficients,
                        target=payload.target,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    result_type=CheckerboardLatticeRender,
                )
                for image in images
            ]
        )
        return list(renders)
