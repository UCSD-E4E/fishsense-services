"""The SimCC keypoint replay's workflows (read-only, owner 2026-10-23).

`ReplayHeadtailKeypointsForMissionWorkflow`: one mission's frames a page at a
time, each page through fishsense-core's SimCC model on the GPU queue
(`PredictHeadtailKeypointsWorkflow`). Only a real GPU runs it (the model was
evaluated under CUDA bf16 autocast). Returns every result: nothing is stored.

`ReplayHeadtailKeypointsWorkflow`: every planned mission, `AT_ONCE` at a
time; one failing stops none of the others. It returns only which child holds
each mission's results (`run collect` reads them), so no result nears
Temporal's payload limit.
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

    from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
    from fishsense_services_contracts.headtail_keypoints import KeypointResult
    from fishsense_services_orchestrator.headtail_keypoints.activities import (
        KeypointPage,
        KeypointTarget,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.nrp.workflow import wake_gpu_processor

__all__ = [
    "AT_ONCE",
    "KeypointMissionResult",
    "KeypointReplayRequest",
    "ReplayHeadtailKeypointsForMissionWorkflow",
    "ReplayHeadtailKeypointsWorkflow",
    "keypoint_child_id",
]

AT_ONCE = 4
_DB = dict(schedule_to_close_timeout=timedelta(minutes=30),
           heartbeat_timeout=timedelta(minutes=2),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@dataclass
class KeypointMissionResult:
    results: List[KeypointResult] = field(default_factory=list)
    #: Frames left out: render gone, or no bucket.
    unavailable: int = 0
    #: No real GPU came up: nothing ran.
    no_gpu: bool = False


@dataclass
class KeypointReplayRequest:
    tenant: str
    #: Mission numbers to replay; empty: every mission with work.
    numbers: List[int] = field(default_factory=list)


def keypoint_child_id(run_id: str, mission_id: str) -> str:
    return f"{run_id}-{mission_id}"


@workflow.defn(name="ReplayHeadtailKeypointsForMissionWorkflow")
class ReplayHeadtailKeypointsForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: KeypointTarget) -> KeypointMissionResult:
        out = KeypointMissionResult()
        after, n, woke = 0, 0, False
        while True:
            page: KeypointPage = await workflow.execute_activity(
                "resolve_keypoint_page", args=(target, after),
                result_type=KeypointPage, **_DB,
            )  # fmt: skip
            if page.last_seq is None:
                return out
            after = page.last_seq
            out.unavailable += page.unavailable
            if page.payload is None or not page.payload.frames:
                continue
            if not woke:
                if await wake_gpu_processor() != MODE_GPU:
                    out.no_gpu = True
                    return out
                woke = True
            out.results += await workflow.execute_child_workflow(
                "PredictHeadtailKeypointsWorkflow",
                page.payload,
                id=f"{workflow.info().workflow_id}-{n}",
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                execution_timeout=timedelta(hours=3),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[KeypointResult],
            )
            n += 1


@workflow.defn(name="ReplayHeadtailKeypointsWorkflow")
class ReplayHeadtailKeypointsWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: KeypointReplayRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_keypoint_replay", args=(request.tenant, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        run_id = workflow.info().workflow_id

        async def one(m: dict) -> dict:
            child = keypoint_child_id(run_id, m["mission_id"])
            row = {"number": m["number"], "mission_id": m["mission_id"],
                   "frames": m["frames"], "child": child}  # fmt: skip
            try:
                await workflow.execute_child_workflow(
                    ReplayHeadtailKeypointsForMissionWorkflow.run,
                    KeypointTarget(uuid.UUID(plan["tenant_id"]), uuid.UUID(m["mission_id"])),
                    id=child, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=KeypointMissionResult,
                )  # fmt: skip
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            return row

        rows: list = []
        missions = plan["missions"]
        for at in range(0, len(missions), AT_ONCE):
            rows += await asyncio.gather(*[one(m) for m in missions[at : at + AT_ONCE]])
        return {"tenant_id": plan["tenant_id"], "missions": rows}
