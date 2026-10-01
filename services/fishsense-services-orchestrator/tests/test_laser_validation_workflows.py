"""Per-dive laser-label validation after the sync, and the remediation run.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_label_studio_laser_labels_workflow.py (the validation tests:
dispatches_validation_child_per_complete_dive,
completes_when_validation_children_fail,
wakes_data_worker_before_dispatching_validation,
does_not_wake_data_worker_when_no_dives_complete),
test_remediate_laser_supersedes_parent_workflow.py and the data-worker's
test_remediate_laser_supersedes_workflow.py. Names and reasons are v1's.

v2 changes, pinned here: each dive is read, judged on the light processor and
written by an orchestrator child of its own (`ValidateDiveLaserLabelsWorkflow`
under v1's id `validate-laser-labels-{dive}`), because the processor never
touches the database; remediation's apply re-plans each dive and refuses,
writing nothing, an id the fresh plan does not contain.
"""

from __future__ import annotations

import uuid
from typing import List

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
from fishsense_services_contracts.laser import (
    DivePlan,
    LaserLabelRow,
    LaserValidationResult,
    PlanLaserRemediationInput,
    RemediateLaserSupersedesInput,
    ValidateLaserLabelsInput,
    revival_digest,
)
from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.labels.workflow import (
    SyncLabelStudioLaserLabelsWorkflow,
)
from fishsense_services_orchestrator.laser import workflow as sut
from fishsense_services_orchestrator.laser.contracts import (
    DiveRemediationRequest,
    LaserTarget,
    RemediationInputs,
    RemediationTarget,
    ReviveLabels,
)
from fishsense_services_orchestrator.laser.stage import STAGE

from ._laser_workflows import StubJudge, StubPlan

QUEUE = "test-laser-validation"
TENANT = uuid.UUID(int=1)


def _target(n):
    return LaserTarget(tenant_id=TENANT, dive_id=uuid.UUID(int=n))


class Script:
    def __init__(self, *, complete=(), fail=(), plans=None, live=()):
        self.complete = list(complete)
        self.fail = set(fail)
        self.plans = plans or {}  # dive number -> revive ids the plan proposes
        self.live = set(live)  # label numbers already live
        self.events: list = []
        self.judged: list = []
        self.revived: list = []

    def activities(self):
        s = self

        @activity.defn(name="laser_label_projects")
        async def projects() -> List[LabelProject]:
            s.events.append("projects")
            return [LabelProject(TENANT, 42)]

        @activity.defn(name="sync_laser_labels")
        async def sync(project: LabelProject) -> None:
            s.events.append("sync")

        @activity.defn(name="laser_dives_with_complete_labeling")
        async def complete() -> List[LaserTarget]:
            s.events.append("complete")
            return s.complete

        @activity.defn(name="ensure_light_processor_running")
        async def wake() -> None:
            s.events.append("wake-light")

        @activity.defn(name="resolve_laser_validation_inputs")
        async def resolve(target: LaserTarget) -> ValidateLaserLabelsInput:
            s.events.append(f"resolve:{target.dive_id.int}")
            return ValidateLaserLabelsInput(dive_id=target.dive_id, labels=[])

        @activity.defn(name="apply_laser_validation")
        async def apply(target: LaserTarget, result: LaserValidationResult) -> int:
            if target.dive_id.int in s.fail:
                raise ApplicationError("simulated failure", non_retryable=True)
            s.events.append(f"apply:{target.dive_id.int}")
            return 1

        @activity.defn(name="_judge")
        async def judge(workflow_id: str, payload: ValidateLaserLabelsInput):
            s.judged.append((workflow_id, activity.info().task_queue))
            return LaserValidationResult(dive_id=payload.dive_id, status="flagged",
                                         positives=10)  # fmt: skip

        @activity.defn(name="resolve_laser_remediation_dives")
        async def dives(numbers) -> List[RemediationTarget]:
            s.events.append(("dives", numbers))
            wanted = sorted(s.plans) if numbers is None else numbers
            return [RemediationTarget(tenant_id=TENANT, dive_id=uuid.UUID(int=n),
                                      number=n) for n in wanted]  # fmt: skip

        @activity.defn(name="resolve_laser_remediation_inputs")
        async def remediation_inputs(
            request: DiveRemediationRequest,
        ) -> RemediationInputs:
            number = request.target.number
            rows = [
                LaserLabelRow(label_id=uuid.uuid4(), number=i, capture_number=i,
                              x=1.0, y=1.0, superseded=i not in s.live,
                              completed=True)
                for i in s.plans.get(number, [])
            ]  # fmt: skip
            return RemediationInputs(
                plan_input=PlanLaserRemediationInput(
                    dive_id=number, labels=rows,
                    excluded_label_ids=request.excluded_label_ids,
                    dive_excluded=request.dive_excluded,
                ),
                fingerprint=f"fp-{number}",
            )  # fmt: skip

        @activity.defn(name="_plan")
        async def plan(payload: PlanLaserRemediationInput) -> DivePlan:
            revive = [] if payload.dive_excluded else [
                r.number for r in payload.labels
                if r.superseded and r.number not in payload.excluded_label_ids
            ]  # fmt: skip
            return DivePlan(dive_id=payload.dive_id, status="flagged",
                            positives=len(payload.labels),
                            superseded_now=len(revive), superseded_after=0,
                            revive_ids=revive)  # fmt: skip

        @activity.defn(name="apply_laser_remediation")
        async def revive(request: ReviveLabels) -> int:
            if unplanned := set(request.pending) - set(request.planned):
                raise ApplicationError(f"unplanned {unplanned}",
                                       type="RemediationPlanMismatch",
                                       non_retryable=True)  # fmt: skip
            s.revived.append((request.target.number, request.pending,
                              request.fingerprint))  # fmt: skip
            return len(request.pending)

        return (
            [projects, sync, complete, wake, resolve, apply, dives,
             remediation_inputs, revive],
            [judge, plan],
        )  # fmt: skip


async def _run(workflow_run, script, arg=None):
    orchestrator, light = script.activities()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(env.client, task_queue=QUEUE,
                   workflows=[*STAGE.workflows, SyncLabelStudioLaserLabelsWorkflow],
                   activities=orchestrator),
            Worker(env.client, task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                   workflows=[StubJudge, StubPlan], activities=light),
        ):  # fmt: skip
            args = () if arg is None else (arg,)
            return await env.client.execute_workflow(
                workflow_run, *args, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


# -- the validation pass after the sync -------------------------------------------


async def test_workflow_dispatches_validation_child_per_complete_dive():
    script = Script(complete=[_target(5), _target(6)])

    await _run(SyncLabelStudioLaserLabelsWorkflow.run, script)

    assert {e for e in script.events if str(e).startswith("apply")} == {
        "apply:5", "apply:6"}  # fmt: skip
    assert sorted(script.judged) == [
        (f"judge-laser-labels-{uuid.UUID(int=n)}", PROCESSOR_LIGHT_TASK_QUEUE)
        for n in (5, 6)
    ]


async def test_workflow_completes_when_validation_children_fail():
    """Validation failures must not roll back a successful sync."""
    script = Script(complete=[_target(5), _target(6)], fail={5})

    await _run(SyncLabelStudioLaserLabelsWorkflow.run, script)

    assert "apply:6" in script.events


async def test_workflow_wakes_the_light_processor_before_dispatching_validation():
    script = Script(complete=[_target(5)])

    await _run(SyncLabelStudioLaserLabelsWorkflow.run, script)

    assert script.events.index("wake-light") < script.events.index("resolve:5")
    assert script.events.index("sync") < script.events.index("complete")


async def test_workflow_does_not_wake_the_light_processor_when_no_dives_complete():
    script = Script(complete=[])

    await _run(SyncLabelStudioLaserLabelsWorkflow.run, script)

    assert "wake-light" not in script.events


async def test_one_dives_validation_is_read_judged_then_written():
    script = Script()

    superseded = await _run(sut.ValidateDiveLaserLabelsWorkflow.run, script, _target(9))

    assert superseded == 1
    assert script.events == ["resolve:9", "apply:9"]


# -- remediation -------------------------------------------------------------------


async def test_a_dry_run_plans_every_dive_and_writes_nothing():
    script = Script(plans={7: [41, 42], 8: [51]})

    report = await _run(
        sut.RemediateLaserSupersedesParentWorkflow.run,
        script,
        RemediateLaserSupersedesInput(dive_ids=[8, 7]),
    )

    assert report["mode"] == "dry_run"
    assert [row["dive_id"] for row in report["dives"]] == [7, 8]
    assert report["totals"]["to_revive"] == 3
    assert report["plan_sha256"] == revival_digest([(7, [41, 42]), (8, [51])])
    assert script.revived == []


async def test_without_dives_it_plans_every_dive():
    script = Script(plans={3: [31], 4: []})

    report = await _run(
        sut.RemediateLaserSupersedesParentWorkflow.run,
        script,
        RemediateLaserSupersedesInput(dive_ids=[], all_dives=True),
    )

    assert ("dives", None) in script.events
    assert [row["dive_id"] for row in report["dives"]] == [3, 4]


async def test_exclusions_reach_the_plan():
    script = Script(plans={7: [41, 42], 8: [51]})

    report = await _run(
        sut.RemediateLaserSupersedesParentWorkflow.run,
        script,
        RemediateLaserSupersedesInput(
            dive_ids=[7, 8], excluded_dive_ids=[8], excluded_label_ids=[42]
        ),
    )

    assert {row["dive_id"]: row["revive_ids"] for row in report["dives"]} == {
        7: [41], 8: []}  # fmt: skip
    assert report["excluded_dive_ids"] == [8]


async def test_apply_without_a_matching_digest_writes_nothing():
    script = Script(plans={7: [41]})

    with pytest.raises(WorkflowFailureError) as raised:
        await _run(
            sut.RemediateLaserSupersedesParentWorkflow.run,
            script,
            RemediateLaserSupersedesInput(
                dive_ids=[7], apply=True, expected_plan_sha256="not-the-digest"
            ),
        )

    assert raised.value.cause.type == "RemediationPlanMismatch"
    assert script.revived == []


async def test_the_apply_flag_alone_is_not_enough():
    script = Script(plans={7: [41]})

    with pytest.raises(WorkflowFailureError):
        await _run(
            sut.RemediateLaserSupersedesParentWorkflow.run,
            script,
            RemediateLaserSupersedesInput(dive_ids=[7], apply=True),
        )
    assert script.revived == []


async def test_apply_with_the_reviewed_digest_writes_exactly_the_plan():
    script = Script(plans={7: [41, 42], 8: [51]})
    digest = revival_digest([(7, [41, 42]), (8, [51])])

    report = await _run(
        sut.RemediateLaserSupersedesParentWorkflow.run,
        script,
        RemediateLaserSupersedesInput(
            dive_ids=[7, 8], apply=True, expected_plan_sha256=digest
        ),
    )

    assert report["mode"] == "apply"
    assert report["applied"] == {"7": 2, "8": 1}
    assert sorted(script.revived) == [(7, [41, 42], "fp-7"), (8, [51], "fp-8")]


async def test_re_applying_after_success_is_a_no_op():
    """Once revived, nothing is left to plan: a clean no-op, not a mismatch."""
    script = Script(plans={7: [41]}, live={41})

    report = await _run(
        sut.RemediateLaserSupersedesParentWorkflow.run,
        script,
        RemediateLaserSupersedesInput(
            dive_ids=[7], apply=True, expected_plan_sha256=revival_digest([(7, [41])])
        ),
    )

    assert report["mode"] == "apply_noop" and script.revived == []
