"""Regroup one dive: the activity and the workflow an operator starts.

New in v2 (2026-10-14). What an operator relies on, pinned here:

* **a dry run by default**: it reports the plan -- every measured frame whose
  fish would change, and the plan's digest -- and writes nothing;
* **an apply names the plan it applies**: without the reviewed digest, or
  with one the dive no longer plans to, it is refused, non-retryably, and
  writes nothing;
* an apply of a dive already grouped that way writes nothing;
* clusters changed between the plan and the write are refused, non-retryably
  (the store checks, under the per-dive lock);
* an unknown dive number is a non-retryable `DiveNotFound`.

    temporal workflow start --task-queue fishsense_orchestrator \\
        --type RegroupDiveWorkflow --input 341                       # dry run
    ... --input 341 --input true --input '"<plan_sha256>"'           # apply
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_api.regroup_store import ClustersChanged
from fishsense_services_orchestrator.ops.regroup.activities import RegroupActivities
from fishsense_services_orchestrator.ops.regroup.contracts import RegroupReport
from fishsense_services_orchestrator.ops.regroup.plan import plan_regroup
from fishsense_services_orchestrator.ops.regroup.workflow import RegroupDiveWorkflow

from .test_regroup_plan import _dive_341

LAB = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
DIVE = uuid.UUID("11111111-2222-4333-8444-555555555555")


class FakeCatalog:
    def __init__(self, facts, *, changed=False):
        self.facts = facts
        self.changed = changed
        self.writes: list[tuple] = []

    async def member_tenants(self):
        return [LAB]

    async def dive_by_number(self, tenant_id, number):
        return DIVE if (tenant_id, number) == (LAB, 341) else None

    async def regroup_facts(self, tenant_id, dive_id):
        assert (tenant_id, dive_id) == (LAB, DIVE)
        return self.facts

    async def replace_label_studio_clusters(
        self, tenant_id, dive_id, clusters, *, expected_fingerprint
    ):
        if self.changed:
            raise ClustersChanged("changed")
        self.writes.append((tenant_id, dive_id, list(clusters), expected_fingerprint))
        return [k.fish_id for k in clusters]


async def _run(catalog, *args) -> RegroupReport:
    return await ActivityEnvironment().run(
        RegroupActivities(catalog=catalog).regroup_dive, *args
    )


async def test_the_dive_alone_is_a_dry_run_that_reports_the_plan():
    catalog = FakeCatalog(_dive_341())

    report = await _run(catalog, 341)

    assert catalog.writes == []
    assert report.applied is False
    assert report.plan_sha256 == plan_regroup(_dive_341()).sha256
    assert [(m.capture_number, m.from_fish, m.to_fish) for m in report.moves] == [
        (302, 305, 307)
    ]
    assert (report.clusters_before, report.clusters_after) == (2, 2)
    assert report.measured_frames == 4


@pytest.mark.parametrize("digest", [None, "0" * 64])
async def test_an_apply_without_the_reviewed_plan_is_refused(digest):
    catalog = FakeCatalog(_dive_341())

    with pytest.raises(ApplicationError) as refused:
        await _run(catalog, 341, True, digest)

    assert refused.value.type == "RegroupPlanMismatch"
    assert refused.value.non_retryable
    assert catalog.writes == []


async def test_an_apply_of_the_reviewed_plan_writes_it():
    facts = _dive_341()
    plan = plan_regroup(facts)
    catalog = FakeCatalog(facts)

    report = await _run(catalog, 341, True, plan.sha256)

    assert report.applied is True
    assert catalog.writes == [(LAB, DIVE, plan.clusters, facts.fingerprint)]


async def test_an_apply_of_a_dive_already_grouped_that_way_writes_nothing():
    facts = _dive_341(
        current=((307, [300, 301, 302, 303]), (305, [434, 437])), stray=307
    )
    catalog = FakeCatalog(facts)

    report = await _run(catalog, 341, True, plan_regroup(facts).sha256)

    assert report.noop and report.applied
    assert catalog.writes == []


async def test_clusters_changed_since_the_plan_are_refused():
    facts = _dive_341()
    catalog = FakeCatalog(facts, changed=True)

    with pytest.raises(ApplicationError) as refused:
        await _run(catalog, 341, True, plan_regroup(facts).sha256)

    assert refused.value.type == "ClustersChanged"
    assert refused.value.non_retryable


async def test_an_unknown_dive_is_not_retried():
    with pytest.raises(ApplicationError) as refused:
        await _run(FakeCatalog(_dive_341()), 9999)

    assert refused.value.type == "DiveNotFound"
    assert refused.value.non_retryable


# -- the workflow ---------------------------------------------------------------

QUEUE = "test-regroup"


async def _execute(args):
    calls: list[tuple] = []

    @activity.defn(name="regroup_dive")
    async def regroup(
        dive_number: int, apply: bool, digest: str | None
    ) -> RegroupReport:
        calls.append((dive_number, apply, digest))
        return RegroupReport(
            dive_number=dive_number, applied=apply, noop=False, plan_sha256="x",
            priority="high", clusters_before=0, clusters_after=0, measured_frames=0,
        )  # fmt: skip

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client, task_queue=QUEUE, workflows=[RegroupDiveWorkflow],
            activities=[regroup],
        ):  # fmt: skip
            report = await env.client.execute_workflow(
                RegroupDiveWorkflow.run, args=args,
                id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE,
            )  # fmt: skip
    return report, calls


async def test_the_workflow_is_a_dry_run_unless_told():
    report, calls = await _execute((341,))
    assert calls == [(341, False, None)]
    assert isinstance(report, RegroupReport) and report.applied is False


async def test_the_workflow_passes_the_apply_and_its_digest():
    _, calls = await _execute((341, True, "abc"))
    assert calls == [(341, True, "abc")]
