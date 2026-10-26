"""The laser-colour backfill's workflows (the owner, 2026-10-26).

`BackfillLaserColoursForMissionWorkflow`: one mission's dots a page at a
time, each page read on the light processor (`ReadLaserColoursWorkflow`) and
recorded (`record_colour_page`: compared, and written only with ``apply``).
Returns the mission's counts.

`BackfillLaserColoursWorkflow`: every planned mission, `AT_ONCE` at a time;
one failing stops none of the others. Its result is the report: each
mission's counts and their totals, so a dry run's numbers are the ones an
apply would act on (the reading is deterministic).
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

    from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
    from fishsense_services_contracts.laser_colour_backfill import ColourReading
    from fishsense_services_orchestrator.laser_colour_backfill.activities import (
        ColourPage,
        ColourTarget,
    )
    from fishsense_services_orchestrator.nrp.workflow import wake_light_processor

__all__ = [
    "AT_ONCE",
    "BackfillLaserColoursForMissionWorkflow",
    "BackfillLaserColoursWorkflow",
    "ColourBackfillRequest",
    "ColourMissionRequest",
    "merge_counts",
]

AT_ONCE = 4
_DB = dict(schedule_to_close_timeout=timedelta(minutes=30),
           heartbeat_timeout=timedelta(minutes=2),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@dataclass
class ColourMissionRequest:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    #: Write the readings still owed; otherwise only count and compare.
    apply: bool = False


@dataclass
class ColourBackfillRequest:
    tenant: str
    #: Mission numbers; empty: every mission with dots.
    numbers: List[int] = field(default_factory=list)
    apply: bool = False


def merge_counts(a: dict, b: dict) -> dict:
    """Sum two count dicts key by key, at every depth."""
    out = dict(a)
    for key, value in b.items():
        if isinstance(value, dict):
            out[key] = merge_counts(out.get(key, {}), value)
        else:
            out[key] = out.get(key, 0) + value
    return out


@workflow.defn(name="BackfillLaserColoursForMissionWorkflow")
class BackfillLaserColoursForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ColourMissionRequest) -> dict:
        target = ColourTarget(request.tenant_id, request.mission_id)
        counts: dict = {"unavailable": 0}
        after, n, woke = 0, 0, False
        while True:
            page: ColourPage = await workflow.execute_activity(
                "resolve_colour_page", args=(target, after),
                result_type=ColourPage, **_DB,
            )  # fmt: skip
            if page.last_seq is None:
                return counts
            after = page.last_seq
            counts["unavailable"] += page.unavailable
            if page.payload is None or not page.payload.frames:
                continue
            if not woke:
                await wake_light_processor()
                woke = True
            readings = await workflow.execute_child_workflow(
                "ReadLaserColoursWorkflow",
                page.payload,
                id=f"{workflow.info().workflow_id}-{n}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                execution_timeout=timedelta(hours=1),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[ColourReading],
            )
            recorded = await workflow.execute_activity(
                "record_colour_page", args=(target, readings, request.apply),
                result_type=dict, **_DB,
            )  # fmt: skip
            counts = merge_counts(counts, recorded)
            n += 1


@workflow.defn(name="BackfillLaserColoursWorkflow")
class BackfillLaserColoursWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ColourBackfillRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_colour_backfill", args=(request.tenant, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        run_id = workflow.info().workflow_id

        async def one(m: dict) -> dict:
            row = {"number": m["number"], "mission_id": m["mission_id"],
                   "frames": m["frames"]}  # fmt: skip
            try:
                row["counts"] = await workflow.execute_child_workflow(
                    BackfillLaserColoursForMissionWorkflow.run,
                    ColourMissionRequest(uuid.UUID(plan["tenant_id"]),
                                         uuid.UUID(m["mission_id"]), request.apply),
                    id=f"{run_id}-{m['mission_id']}",
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=dict,
                )  # fmt: skip
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            return row

        rows: list = []
        missions = plan["missions"]
        for at in range(0, len(missions), AT_ONCE):
            rows += await asyncio.gather(*[one(m) for m in missions[at : at + AT_ONCE]])
        totals: dict = {}
        for row in rows:
            totals = merge_counts(totals, row.get("counts", {}))
        return {"tenant_id": plan["tenant_id"], "apply": request.apply,
                "missions": rows, "totals": totals,
                "failed_missions": sum(1 for r in rows if "error" in r)}  # fmt: skip
