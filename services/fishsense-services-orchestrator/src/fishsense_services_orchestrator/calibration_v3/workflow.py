"""Calibration v3's read-only replay.

`ReplayCalibrationV3ForMissionWorkflow`: the mission's calibration inputs, as
the automatic chain resolves them (`resolve_automatic_calibration_inputs`,
read-only), then v3 on the per-image role (the processor's
`ReplayAutomaticCalibrationV3Workflow`)
-- with draws of its slate frames when asked. Its result is the report.

`ReplayCalibrationV3Workflow`: every planned mission, `AT_ONCE` at a time; it
returns only which child holds each mission's report (`collect` in `replay`
reads them), so no result nears Temporal's payload limit. Nothing is written.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.calibration_v3 import (
        CalibrationV3Report,
        ReplayCalibrationInput,
    )
    from fishsense_services_orchestrator.automatic_results.activities import (
        AutomaticCalibrationPlan,
        AutomaticTarget,
    )
    from fishsense_services_orchestrator.nrp.workflow import wake_per_image_processor

__all__ = [
    "AT_ONCE",
    "ReplayCalibrationV3ForMissionWorkflow",
    "ReplayCalibrationV3Workflow",
    "ReplayRequest",
    "ReplayTarget",
    "replay_child_id",
]

AT_ONCE = 4
_DB = dict(schedule_to_close_timeout=timedelta(minutes=15),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@dataclass
class ReplayTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    draw_sizes: List[int] = field(default_factory=list)
    draws_per_size: int = 0


@dataclass
class ReplayRequest:
    tenant: str
    #: Mission numbers to replay; empty: every mission with an automatic dot.
    numbers: List[int] = field(default_factory=list)
    #: Missions whose slate frames are also drawn from (the small-n control).
    draw_numbers: List[int] = field(default_factory=list)
    draw_sizes: List[int] = field(default_factory=list)
    draws_per_size: int = 0


def replay_child_id(run_id: str, mission_id: str) -> str:
    return f"calibration-v3-replay-{run_id}-{mission_id}"


@workflow.defn(name="ReplayCalibrationV3ForMissionWorkflow")
class ReplayCalibrationV3ForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: ReplayTarget) -> CalibrationV3Report:
        plan: AutomaticCalibrationPlan = await workflow.execute_activity(
            "resolve_automatic_calibration_inputs",
            AutomaticTarget(target.tenant_id, target.mission_id),
            result_type=AutomaticCalibrationPlan, **_DB,
        )  # fmt: skip
        if not plan.payload.frames:
            return CalibrationV3Report(mission_id=target.mission_id, outcome="refused",
                                       refusal_reason="no_candidates")  # fmt: skip
        await wake_per_image_processor()
        return await workflow.execute_child_workflow(
            "ReplayAutomaticCalibrationV3Workflow",
            ReplayCalibrationInput(payload=plan.payload, draw_sizes=target.draw_sizes,
                                   draws_per_size=target.draws_per_size),
            id=f"{workflow.info().workflow_id}-fit",
            task_queue=PROCESSOR_TASK_QUEUE,
            execution_timeout=timedelta(hours=3),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=CalibrationV3Report,
        )  # fmt: skip


@workflow.defn(name="ReplayCalibrationV3Workflow")
class ReplayCalibrationV3Workflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ReplayRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_calibration_v3_replay", args=(request.tenant, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        run_id = workflow.info().workflow_id
        rows: list = []

        async def one(m: dict) -> dict:
            draw = m["number"] in set(request.draw_numbers)
            child = replay_child_id(run_id, m["mission_id"])
            row = {"number": m["number"], "mission_id": m["mission_id"], "child": child}
            try:
                await workflow.execute_child_workflow(
                    ReplayCalibrationV3ForMissionWorkflow.run,
                    ReplayTarget(uuid.UUID(plan["tenant_id"]), uuid.UUID(m["mission_id"]),
                                 request.draw_sizes if draw else [],
                                 request.draws_per_size if draw else 0),
                    id=child, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=CalibrationV3Report,
                )  # fmt: skip
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            return row

        missions = plan["missions"]
        for at in range(0, len(missions), AT_ONCE):
            rows += await asyncio.gather(*[one(m) for m in missions[at : at + AT_ONCE]])
        return {"tenant_id": plan["tenant_id"], "missions": rows}
