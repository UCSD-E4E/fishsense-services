"""The phone species' workflows (the owner, 2026-11-11).

`PhoneSpeciesForMissionWorkflow`: one mission's phone fish owed a species, a
page at a time: resolve (presigned photos, mask boxes, the region's fish),
classify on the GPU queue with the species stage's processor workflow
(`PredictSpeciesImagesWorkflow`, unchanged), persist. The GPU queue is woken
once there is work, and a run that can't get a worker for it dispatches
nothing (`wake_gpu_processor`: an unserved queue hangs a child for hours).
Returns the outcomes counted.

`PhoneSpeciesRunWorkflow`: dry run, the plan (the region, the missions owed
species, how many each, the digest); apply, re-planned and refused unless
the digest still matches, then each mission one after another; one failing
stops none of the others. Nothing is written in a dry run.
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
    from fishsense_services_contracts.species_prediction import (
        SpeciesPredictionResult,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_UNAVAILABLE
    from fishsense_services_orchestrator.nrp.workflow import wake_gpu_processor
    from fishsense_services_orchestrator.phone_species.activities import (
        PhoneSpeciesPage,
        PhoneSpeciesTarget,
    )

__all__ = [
    "PhoneSpeciesForMissionWorkflow",
    "PhoneSpeciesRequest",
    "PhoneSpeciesRunWorkflow",
    "plan_digest",
]

_DB = dict(schedule_to_close_timeout=timedelta(minutes=30),
           heartbeat_timeout=timedelta(minutes=2),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip

#: BioCLIP on a page of 100 crops: minutes on a GPU, longer on the CPU
#: fallback; generous, as an unserved child would otherwise hang.
CHILD_TIMEOUT = timedelta(hours=6)


def _add(counts: Dict[str, int], key: str, n: int) -> None:
    if n:
        counts[key] = counts.get(key, 0) + n


@workflow.defn(name="PhoneSpeciesForMissionWorkflow")
class PhoneSpeciesForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: PhoneSpeciesTarget) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        after: Optional[str] = None
        n, mode = 0, None
        while True:
            page: PhoneSpeciesPage = await workflow.execute_activity(
                "resolve_phone_species_page", args=(target, after),
                result_type=PhoneSpeciesPage, **_DB,
            )  # fmt: skip
            if page.last is None:
                break
            after = page.last
            _add(counts, "no_bucket", page.unavailable)
            _add(counts, "unreadable_mask", page.unreadable)
            if page.payload is None or not page.payload.images:
                continue
            if mode is None:
                mode = await wake_gpu_processor()
            if mode == MODE_UNAVAILABLE:
                raise ApplicationError("no worker for the GPU queue; nothing sent",
                                       type="GpuUnavailable", non_retryable=True)  # fmt: skip
            results: List[SpeciesPredictionResult] = (
                await workflow.execute_child_workflow(
                    "PredictSpeciesImagesWorkflow",
                    page.payload,
                    id=f"phone-species-{target.mission_id}-{n}",
                    task_queue=PROCESSOR_GPU_TASK_QUEUE,
                    execution_timeout=CHILD_TIMEOUT,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=List[SpeciesPredictionResult],
                )
            )
            n += 1
            _add(counts, "left_out", len(page.payload.images) - len(results))
            for r in results:
                _add(counts, r.status, 1)
            recorded = await workflow.execute_activity(
                "persist_phone_species", args=(target, results), result_type=int, **_DB
            )
            _add(counts, "recorded", recorded)
        return counts


@dataclass
class PhoneSpeciesRequest:
    tenant: str
    #: The broad region the photos were taken in (`regions`), which names
    #: the fish offered.
    region: str
    #: Mission numbers to restrict to; empty: every mission owed species.
    numbers: List[int] = field(default_factory=list)
    apply: bool = False
    expected_plan_sha256: Optional[str] = None


def plan_digest(plan: dict) -> str:
    """The reviewed plan's identity: the tenant, the region and its missions
    -- not the counts, which fall as fish are named."""
    key = [plan["tenant_id"], plan["region"],
           sorted(m["mission_id"] for m in plan["missions"])]  # fmt: skip
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


@workflow.defn(name="PhoneSpeciesRunWorkflow")
class PhoneSpeciesRunWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: PhoneSpeciesRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_phone_species", args=(request.tenant, request.region, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        digest = plan_digest(plan)
        report: dict = {
            "mode": "apply" if request.apply else "dry_run", "tenant": request.tenant,
            "region": request.region, "numbers": list(request.numbers), "plan": plan,
            "plan_sha256": digest,
            "captures": sum(m["captures"] for m in plan["missions"]), "missions": [],
        }  # fmt: skip
        if not request.apply:
            return report
        if digest != request.expected_plan_sha256:
            raise ApplicationError(
                f"the plan changed since its review ({request.expected_plan_sha256} "
                f"-> {digest}): dry-run again", type="PlanChanged", non_retryable=True,
            )  # fmt: skip
        for mission in plan["missions"]:
            target = PhoneSpeciesTarget(uuid.UUID(plan["tenant_id"]),
                                        uuid.UUID(mission["mission_id"]),
                                        plan["region"])  # fmt: skip
            row: dict = {"number": mission["number"],
                         "mission_id": mission["mission_id"]}  # fmt: skip
            try:
                row["counts"] = await workflow.execute_child_workflow(
                    PhoneSpeciesForMissionWorkflow.run,
                    target,
                    id=f"phone-species-{mission['mission_id']}",
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=Dict[str, int],
                )
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            except WorkflowAlreadyStartedError:
                row["error"] = "running_elsewhere"
            report["missions"].append(row)
        return report
