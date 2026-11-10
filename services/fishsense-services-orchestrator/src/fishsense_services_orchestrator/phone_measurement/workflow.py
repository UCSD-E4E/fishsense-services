"""The phone measurement's workflows (contract 28).

`PhoneMeasurementForMissionWorkflow`: one mission's phone captures owed a
measurement, a page at a time -- resolve (presigned files and mask uploads),
measure on the per-image queue (`MeasurePhoneCapturesWorkflow`), persist.
The per-image processor is woken once there is work. Returns the outcomes
counted.

`PhoneMeasurementRunWorkflow`: dry run, the plan (the missions owed
measurements, how many each, the digest); apply, re-planned and refused
unless the digest still matches, then each mission one after another -- one
failing stops none of the others. Nothing is written in a dry run.
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

    from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
    from fishsense_services_contracts.phone_measurement import PhoneMeasured
    from fishsense_services_orchestrator.nrp.workflow import wake_per_image_processor
    from fishsense_services_orchestrator.phone_measurement.activities import (
        PhoneMeasurementPage,
        PhoneMeasurementTarget,
    )

__all__ = [
    "PhoneMeasurementForMissionWorkflow",
    "PhoneMeasurementRequest",
    "PhoneMeasurementRunWorkflow",
    "plan_digest",
]

_DB = dict(schedule_to_close_timeout=timedelta(minutes=30),
           heartbeat_timeout=timedelta(minutes=2),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@workflow.defn(name="PhoneMeasurementForMissionWorkflow")
class PhoneMeasurementForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: PhoneMeasurementTarget) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        after: Optional[str] = None
        n, woke = 0, False
        while True:
            page: PhoneMeasurementPage = await workflow.execute_activity(
                "resolve_phone_measurement_page", args=(target, after),
                result_type=PhoneMeasurementPage, **_DB,
            )  # fmt: skip
            if page.last is None:
                break
            after = page.last
            if page.no_bucket:
                counts["no_bucket"] = counts.get("no_bucket", 0) + page.unavailable
            if page.payload is None or not page.payload.frames:
                continue
            if not woke:
                await wake_per_image_processor()
                woke = True
            results: List[PhoneMeasured] = await workflow.execute_child_workflow(
                "MeasurePhoneCapturesWorkflow",
                page.payload,
                id=f"phone-measurement-{target.mission_id}-{n}",
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=timedelta(hours=6),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[PhoneMeasured],
            )
            n += 1
            left_out = len(page.payload.frames) - len(results)
            if left_out:
                counts["left_out"] = counts.get("left_out", 0) + left_out
            for r in results:
                counts[r.status] = counts.get(r.status, 0) + 1
            recorded = await workflow.execute_activity(
                "persist_phone_measurements",
                args=(target, results),
                result_type=int,
                **_DB,
            )
            counts["recorded"] = counts.get("recorded", 0) + recorded
        return counts


@dataclass
class PhoneMeasurementRequest:
    tenant: str
    #: Mission numbers to restrict to; empty: every mission owed measurements.
    numbers: List[int] = field(default_factory=list)
    apply: bool = False
    expected_plan_sha256: Optional[str] = None


def plan_digest(plan: dict) -> str:
    """The reviewed plan's identity: the tenant and its missions -- not the
    counts, which fall as captures are measured."""
    key = [plan["tenant_id"], sorted(m["mission_id"] for m in plan["missions"])]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


@workflow.defn(name="PhoneMeasurementRunWorkflow")
class PhoneMeasurementRunWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: PhoneMeasurementRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_phone_measurement", args=(request.tenant, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        digest = plan_digest(plan)
        report: dict = {
            "mode": "apply" if request.apply else "dry_run", "tenant": request.tenant,
            "numbers": list(request.numbers), "plan": plan, "plan_sha256": digest,
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
            target = PhoneMeasurementTarget(uuid.UUID(plan["tenant_id"]),
                                            uuid.UUID(mission["mission_id"]))  # fmt: skip
            row: dict = {
                "number": mission["number"],
                "mission_id": mission["mission_id"],
            }
            try:
                row["counts"] = await workflow.execute_child_workflow(
                    PhoneMeasurementForMissionWorkflow.run,
                    target,
                    id=f"phone-measurement-{mission['mission_id']}",
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=Dict[str, int],
                )
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            except WorkflowAlreadyStartedError:
                row["error"] = "running_elsewhere"
            report["missions"].append(row)
        return report
