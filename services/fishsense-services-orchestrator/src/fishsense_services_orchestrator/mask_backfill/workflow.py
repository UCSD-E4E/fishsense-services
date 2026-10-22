"""The mask backfill's workflows.

`MaskBackfillForMissionWorkflow`: one mission's owed predictions, a page at
a time -- resolve (images and mask uploads), regenerate on the GPU queue
(`BackfillMasksWorkflow`), persist the reproduced masks. The GPU is woken
once there is work, and **only a real GPU runs it**: the CPU fallback serves
the queue with Mask R-CNN, which made none of these masks. Returns the
outcomes counted.

`MaskBackfillRunWorkflow`: dry run, the plan (the missions owed masks, how
many each, the digest, the split into parts); apply, re-planned and refused
unless the digest still matches, then its part's missions one after another
-- one failing stops none of the others, a GPU that won't come up stops the
run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
    from fishsense_services_contracts.mask_backfill import BackfilledMask
    from fishsense_services_orchestrator.mask_backfill.activities import (
        MaskBackfillPage,
        MaskBackfillTarget,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.nrp.workflow import wake_gpu_processor

__all__ = [
    "MaskBackfillForMissionWorkflow",
    "MaskBackfillRequest",
    "MaskBackfillRunWorkflow",
    "plan_digest",
    "split",
]

_DB = dict(schedule_to_close_timeout=timedelta(minutes=30),
           heartbeat_timeout=timedelta(minutes=2),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@workflow.defn(name="MaskBackfillForMissionWorkflow")
class MaskBackfillForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: MaskBackfillTarget) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        after, n, woke = 0, 0, False
        while True:
            page: MaskBackfillPage = await workflow.execute_activity(
                "resolve_mask_backfill_page", args=(target, after),
                result_type=MaskBackfillPage, **_DB,
            )  # fmt: skip
            if page.last_seq is None:
                break
            after = page.last_seq
            if page.unavailable:
                key = "no_bucket" if page.no_bucket else "image_unavailable"
                counts[key] = counts.get(key, 0) + page.unavailable
            if page.payload is None or not page.payload.frames:
                continue
            if not woke:
                if await wake_gpu_processor() != MODE_GPU:
                    counts["no_gpu"] = 1
                    return counts
                woke = True
            results: List[BackfilledMask] = await workflow.execute_child_workflow(
                "BackfillMasksWorkflow",
                page.payload,
                id=f"mask-backfill-{target.source}-{target.mission_id}-{n}",
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                execution_timeout=timedelta(hours=6),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[BackfilledMask],
            )
            n += 1
            left_out = len(page.payload.frames) - len(results)
            if left_out:
                counts["left_out"] = counts.get("left_out", 0) + left_out
            for r in results:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
            named = await workflow.execute_activity(
                "persist_mask_backfill", args=(target, results), result_type=int, **_DB
            )
            counts["named"] = counts.get("named", 0) + named
        return counts


@dataclass
class MaskBackfillRequest:
    tenant: str
    source: str
    #: Mission numbers to restrict to; empty: every mission owed masks.
    numbers: List[int] = field(default_factory=list)
    apply: bool = False
    expected_plan_sha256: Optional[str] = None
    #: This run's share of the plan's missions: part `part` of `parts`,
    #: balanced by predictions (`split`), so parts can run side by side.
    part: int = 0
    parts: int = 1


def plan_digest(plan: dict) -> str:
    """The reviewed plan's identity: tenant, source and missions -- not the
    counts, which fall as masks are named."""
    key = [plan["tenant_id"], plan["source"],
           sorted(m["mission_id"] for m in plan["missions"])]  # fmt: skip
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


def split(missions: List[dict], parts: int) -> List[List[dict]]:
    """The missions in `parts` shares of about equal predictions (largest
    first to the lightest share), each share in mission-number order."""
    shares: List[List[dict]] = [[] for _ in range(parts)]
    loads = [0] * parts
    for m in sorted(missions, key=lambda m: (-m["predictions"], m["number"])):
        i = loads.index(min(loads))
        shares[i].append(m)
        loads[i] += m["predictions"]
    return [sorted(s, key=lambda m: m["number"]) for s in shares]


@workflow.defn(name="MaskBackfillRunWorkflow")
class MaskBackfillRunWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: MaskBackfillRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_mask_backfill", args=(request.tenant, request.source, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        digest = plan_digest(plan)
        shares = split(plan["missions"], request.parts)
        report: dict = {
            "mode": "apply" if request.apply else "dry_run", "tenant": request.tenant,
            "source": request.source, "numbers": list(request.numbers), "plan": plan,
            "plan_sha256": digest,
            "parts": [[m["number"] for m in s] for s in shares],
            "missions": [], "stopped": None,
        }  # fmt: skip
        if not request.apply:
            return report
        if digest != request.expected_plan_sha256:
            raise ApplicationError(
                f"the plan changed since its review ({request.expected_plan_sha256} "
                f"-> {digest}): dry-run again", type="PlanChanged", non_retryable=True,
            )  # fmt: skip
        for mission in shares[request.part]:
            target = MaskBackfillTarget(uuid.UUID(plan["tenant_id"]),
                                        uuid.UUID(mission["mission_id"]), request.source)  # fmt: skip
            row: dict = {
                "number": mission["number"],
                "mission_id": mission["mission_id"],
            }
            try:
                row["counts"] = await workflow.execute_child_workflow(
                    MaskBackfillForMissionWorkflow.run,
                    target,
                    id=f"mask-backfill-{request.source}-{mission['mission_id']}",
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=Dict[str, int],
                )
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            except WorkflowAlreadyStartedError:
                row["error"] = "running_elsewhere"
            report["missions"].append(row)
            if row.get("counts", {}).get("no_gpu"):
                report["stopped"] = "no GPU"
                break  # every mission behind it would wait out the wake too
        return report
