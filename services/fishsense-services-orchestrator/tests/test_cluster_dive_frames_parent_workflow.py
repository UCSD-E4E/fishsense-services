"""Workflow contract test for ClusterDiveFramesParentWorkflow.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_cluster_dive_frames_parent_workflow.py. Test names, bodies and reasons are
v1's; v2 adaptations: the target is (tenant, dive), ids are UUIDs, the child
goes to the processor's light queue, and there is no NRP scale-up step yet
(PLAN.md §6.2's worker duties port separately).

v2 fix, pinned last: **a completed child with the same id must not stop the
persist.** v1 dispatched with ALLOW_DUPLICATE_FAILED_ONLY and returned early on
WorkflowAlreadyStartedError, so a child that completed before its parent's
persist failed blocked every later firing: the dive, still without clusters,
was re-selected hourly and never clustered.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_LIGHT_TASK_QUEUE,
    ClusterDiveFrameImage,
    ClusterDiveFramesInput,
)
from fishsense_services_orchestrator.clustering.activities import ClusteringTarget
from fishsense_services_orchestrator.clustering.workflow import (
    ClusterDiveFramesParentWorkflow,
)

_BASE = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
A, B, C, D = (uuid.UUID(int=i) for i in range(1, 5))


@workflow.defn(name="DiveFrameClusteringWorkflow")
class _StubChildWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: ClusterDiveFramesInput) -> List[List[uuid.UUID]]:
        await workflow.execute_activity(
            "_record_child_dispatch",
            args=(
                workflow.info().workflow_id,
                payload.dive_id,
                [img.capture_id for img in payload.images],
            ),
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        # Return two synthetic clusters so persist gets non-trivial input.
        return [[A, B], [C, D]]


def _make_recording_activity(captures: List[tuple]):
    @activity.defn(name="_record_child_dispatch")
    async def record_child_dispatch(
        workflow_id: str, dive_id: uuid.UUID, capture_ids: List[uuid.UUID]
    ) -> None:
        captures.append((workflow_id, dive_id, capture_ids))

    return record_child_dispatch


def _make_stubs(selector_result, resolver_result, persist_calls: List[tuple]):
    @activity.defn(name="select_next_dive_for_clustering")
    async def stub_select() -> ClusteringTarget | None:
        return selector_result

    @activity.defn(name="resolve_clustering_inputs")
    async def stub_resolve(target: ClusteringTarget) -> ClusterDiveFramesInput:
        assert resolver_result is not None
        return resolver_result

    @activity.defn(name="persist_prediction_clusters")
    async def stub_persist(
        target: ClusteringTarget, clusters: List[List[uuid.UUID]]
    ) -> int:
        persist_calls.append((target, clusters))
        return len(clusters)

    return [stub_select, stub_resolve, stub_persist]


async def _run(selector_result, resolver_result, *, before=None):
    persist_calls: List[tuple] = []
    child_runs: List[tuple] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue="test-stage1-parent",
                workflows=[ClusterDiveFramesParentWorkflow],
                activities=_make_stubs(selector_result, resolver_result, persist_calls),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                workflows=[_StubChildWorkflow],
                activities=[_make_recording_activity(child_runs)],
            ),
        ):
            if before:
                await before(env.client)
            result = await env.client.execute_workflow(
                ClusterDiveFramesParentWorkflow.run,
                id=f"test-stage1-parent-{uuid.uuid4()}",
                task_queue="test-stage1-parent",
            )
    return result, child_runs, persist_calls


def _inputs(images=True):
    return ClusterDiveFramesInput(
        dive_id=DIVE,
        images=(
            [
                ClusterDiveFrameImage(capture_id=A, taken_datetime=_BASE),
                ClusterDiveFrameImage(
                    capture_id=B, taken_datetime=_BASE + timedelta(seconds=1)
                ),
            ]
            if images
            else []
        ),
    )


TARGET = ClusteringTarget(tenant_id=TENANT, dive_id=DIVE)


async def test_dispatches_child_with_deterministic_id_and_persists_clusters():
    result, child_runs, persist_calls = await _run(TARGET, _inputs())

    assert result == TARGET
    assert len(child_runs) == 1
    child_id, child_dive_id, capture_ids = child_runs[0]
    assert child_id == f"cluster-{DIVE}"
    assert child_dive_id == DIVE
    assert capture_ids == [A, B]
    assert persist_calls == [(TARGET, [[A, B], [C, D]])]


async def test_returns_none_when_no_dive():
    result, child_runs, persist_calls = await _run(None, None)

    assert result is None
    assert not child_runs
    assert not persist_calls


async def test_skips_child_and_persist_when_no_images():
    result, child_runs, persist_calls = await _run(TARGET, _inputs(images=False))

    assert result == TARGET
    assert not child_runs
    assert not persist_calls


async def test_a_completed_child_with_the_same_id_does_not_stop_the_persist():
    """v2 fix. A prior firing's child completed, then its parent's persist
    failed: the dive is re-selected with no clusters. v1 hit "already started"
    here and returned without persisting, forever. Clustering is deterministic
    and cheap, so the child re-runs and the (idempotent) persist happens."""

    async def a_prior_child_completed(client):
        await client.execute_workflow(
            "DiveFrameClusteringWorkflow",
            _inputs(),
            id=f"cluster-{DIVE}",
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
            result_type=list,
        )

    result, child_runs, persist_calls = await _run(
        TARGET, _inputs(), before=a_prior_child_completed
    )

    assert result == TARGET
    assert len(child_runs) == 2  # the prior one, then this firing's
    assert persist_calls == [(TARGET, [[A, B], [C, D]])]
