"""Stand-in processor workflows for the laser parents' tests, and a blocker
that holds a child id open. Kept apart from the test modules so the workflow
sandbox imports only what a real parent's children would."""

from __future__ import annotations

from datetime import timedelta
from typing import List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts.laser import (
        DivePlan,
        EvaluateLaserAutoAcceptInput,
        LaserAutoAcceptResult,
        LaserPredictionResult,
        LaserValidationResult,
        PlanLaserRemediationInput,
        PredictLaserImagesInput,
        PreprocessLaserImagesInput,
        ValidateLaserLabelsInput,
    )

_RECORD = {"schedule_to_close_timeout": timedelta(seconds=10)}


@workflow.defn(name="PreprocessLaserImagesWorkflow")
class StubPreprocess:
    @workflow.run
    async def run(self, payload: PreprocessLaserImagesInput) -> None:
        await workflow.execute_activity(
            "_child", args=("preprocess", workflow.info().workflow_id), **_RECORD
        )


@workflow.defn(name="PredictLaserImagesWorkflow")
class StubPredict:
    @workflow.run
    async def run(
        self, payload: PredictLaserImagesInput
    ) -> List[LaserPredictionResult]:
        return await workflow.execute_activity(
            "_predictions",
            args=(workflow.info().workflow_id, payload),
            result_type=List[LaserPredictionResult],
            **_RECORD,
        )


@workflow.defn(name="EvaluateLaserAutoAcceptWorkflow")
class StubGate:
    @workflow.run
    async def run(self, payload: EvaluateLaserAutoAcceptInput) -> LaserAutoAcceptResult:
        return await workflow.execute_activity(
            "_gate",
            args=(workflow.info().workflow_id, payload),
            result_type=LaserAutoAcceptResult,
            **_RECORD,
        )


@workflow.defn(name="ValidateLaserLabelsForDiveWorkflow")
class StubJudge:
    @workflow.run
    async def run(self, payload: ValidateLaserLabelsInput) -> LaserValidationResult:
        return await workflow.execute_activity(
            "_judge",
            args=(workflow.info().workflow_id, payload),
            result_type=LaserValidationResult,
            **_RECORD,
        )


@workflow.defn(name="PlanLaserSupersedeRemediationWorkflow")
class StubPlan:
    @workflow.run
    async def run(self, payload: PlanLaserRemediationInput) -> DivePlan:
        return await workflow.execute_activity(
            "_plan", args=(payload,), result_type=DivePlan, **_RECORD
        )


@workflow.defn
class Blocker:
    """Holds a workflow id open until signalled."""

    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal
    def finish(self) -> None:
        self._done = True
