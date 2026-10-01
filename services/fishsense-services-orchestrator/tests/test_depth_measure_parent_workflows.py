"""Workflow contract tests for the laser-depth and measure-fish parents.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_measure_fish_parent_workflow.py; v1 had no test for
ComputeLaserDepthsParentWorkflow (the map says write it first), so the same
contract is pinned for both. v1's:

1. selector returns None -> the parent returns None, no child dispatch;
2. selector returns a dive -> the child is dispatched on the processor's
   light queue with a deterministic id and the dive's payload.

v2 adaptations: the target is (tenant, dive) and the child's id is
tenant-scoped (PLAN.md §4.5); the payload is the resolved contract input, not
a bare dive id; the NRP wake is `ensure_light_processor_running`, which in v2
stands the light processor up. v2 changes, pinned:

* the parent **persists what the child returns**, under the calibration it
  resolved (v1's child wrote through the SDK);
* no work, no wake and no child (as v1: a quiet hour never wakes a pod);
* a *completed* child with the same id does not block the run (v1's
  ALLOW_DUPLICATE, dive 59); a *running* one persists instead of this run;
* the run returns its counters, not only the dive.
"""

from __future__ import annotations

import uuid
from typing import List

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from ._depth_measure_children import (
    HangingDepthChild,
    StubDepthChild,
    StubMeasureChild,
    depth_result,
    measure_result,
)
from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserCalibrationGeometry,
    LaserDepthCapture,
    LaserDot,
)
from fishsense_services_contracts.measurement import (
    HeadTail,
    MeasureFishCapture,
    MeasureFishInput,
    MeasureFishResult,
)
from fishsense_services_orchestrator.laser_depth.activities import (
    DiveTarget,
    LaserDepthResolution,
    PersistedLaserDepths,
)
from fishsense_services_orchestrator.laser_depth.workflow import (
    ComputeLaserDepthsParentWorkflow,
    LaserDepthsRun,
)
from fishsense_services_orchestrator.measurement.activities import (
    MeasurementResolution,
    PersistedMeasurementRun,
)
from fishsense_services_orchestrator.measurement.workflow import (
    MeasureFishParentWorkflow,
    MeasureFishRun,
)

TENANT, DIVE = uuid.uuid4(), uuid.uuid4()
TARGET = DiveTarget(tenant_id=TENANT, dive_id=DIVE)
CALIBRATION = uuid.uuid4()
K = ((3000.0, 0.0, 2048.0), (0.0, 3000.0, 1536.0), (0.0, 0.0, 1.0))
GEOMETRY = LaserCalibrationGeometry(
    laser_calibration_id=CALIBRATION,
    laser_position=(0.1, 0.0, 0.0),
    laser_axis=(0.0, 0.0, 1.0),
)
CAPTURE = uuid.uuid4()
DEPTH_INPUT = ComputeLaserDepthsInput(
    dive_id=DIVE,
    camera_matrix=K,
    calibration=GEOMETRY,
    captures=[
        LaserDepthCapture(
            capture_id=CAPTURE,
            laser_labels=[LaserDot(laser_label_id=uuid.uuid4(), x=1.0, y=2.0)],
        )
    ],
)
MEASURE_INPUT = MeasureFishInput(
    dive_id=DIVE,
    camera_matrix=K,
    calibration=GEOMETRY,
    captures=[
        MeasureFishCapture(
            capture_id=CAPTURE,
            species_label_id=uuid.uuid4(),
            laser=LaserDot(laser_label_id=uuid.uuid4(), x=1.0, y=2.0),
            head_tail=HeadTail(
                head_tail_label_id=uuid.uuid4(),
                head_x=1.0,
                head_y=2.0,
                tail_x=3.0,
                tail_y=4.0,
            ),
        )
    ],
)
DEPTH_RESULT = depth_result(DEPTH_INPUT)
MEASURE_RESULT = measure_result(MEASURE_INPUT)


class Stage:
    def __init__(self, kind: str, selected, resolution):
        self.kind = kind
        self.selected = selected
        self.resolution = resolution
        self.events: List[str] = []
        self.children: List[tuple] = []
        self.persisted: List[tuple] = []

    def activities(self):
        names = {
            "depth": (
                "select_next_dive_for_laser_depth",
                "resolve_laser_depth_inputs",
                "persist_laser_depths",
            ),
            "measure": (
                "select_next_dive_for_measurement",
                "resolve_measurement_inputs",
                "persist_measurements",
            ),
        }[self.kind]

        @activity.defn(name="ensure_light_processor_running")
        async def wake() -> int:
            self.events.append("wake")
            return 1

        @activity.defn(name=names[0])
        async def select() -> DiveTarget | None:
            return self.selected

        if self.kind == "depth":

            @activity.defn(name=names[1])
            async def resolve(target: DiveTarget) -> LaserDepthResolution:
                return self.resolution

            @activity.defn(name=names[2])
            async def persist(
                target: DiveTarget,
                calibration: uuid.UUID,
                result: ComputeLaserDepthsResult,
            ) -> PersistedLaserDepths:
                self.events.append("persist")
                self.persisted.append((target, calibration, result))
                return PersistedLaserDepths(computed=1, refused=2, skipped_stale=0)

        else:

            @activity.defn(name=names[1])
            async def resolve(target: DiveTarget) -> MeasurementResolution:
                return self.resolution

            @activity.defn(name=names[2])
            async def persist(
                target: DiveTarget, calibration: uuid.UUID, result: MeasureFishResult
            ) -> PersistedMeasurementRun:
                self.events.append("persist")
                self.persisted.append((target, calibration, result))
                return PersistedMeasurementRun(
                    measured=1,
                    refused=2,
                    skipped_stale=0,
                    fish_created=1,
                    clusters_bound=1,
                )

        return [wake, select, resolve, persist]

    def recorder(self):
        @activity.defn(name="_record_child_dispatch")
        async def record(workflow_id: str, dive_id: uuid.UUID, captures: int) -> None:
            self.events.append("child")
            self.children.append((workflow_id, dive_id, captures))

        return record


PARENTS = {
    "depth": ComputeLaserDepthsParentWorkflow,
    "measure": MeasureFishParentWorkflow,
}
CHILDREN = {"depth": StubDepthChild, "measure": StubMeasureChild}
CHILD_IDS = {
    "depth": f"compute-laser-depths-{TENANT}-{DIVE}",
    "measure": f"measure-fish-{TENANT}-{DIVE}",
}


def _resolution(kind, payload=True):
    if kind == "depth":
        return LaserDepthResolution(
            payload=DEPTH_INPUT if payload else None,
            skipped_current=3,
            skipped_refused=1,
            skipped_unusable_label=2,
        )
    return MeasurementResolution(
        payload=MEASURE_INPUT if payload else None,
        skipped_already_measured=3,
        skipped_unmeasurable_species=4,
        missing_cluster=5,
        missing_laser_or_headtail=6,
        skipped_refused=1,
    )


async def _run(stage: Stage, *, child=None, before=None):
    parent = PARENTS[stage.kind]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue="test-parent",
                workflows=[parent],
                activities=stage.activities(),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                workflows=[child or CHILDREN[stage.kind]],
                activities=[stage.recorder()],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                parent.run,
                id=f"test-parent-{uuid.uuid4()}",
                task_queue="test-parent",
            )


@pytest.mark.parametrize("kind", ["depth", "measure"])
async def test_dispatches_child_with_deterministic_id_and_dive_payload(kind):
    stage = Stage(kind, TARGET, _resolution(kind))

    result = await _run(stage)

    assert stage.children == [(CHILD_IDS[kind], DIVE, 1)]
    assert stage.events == ["wake", "child", "persist"]
    ((target, calibration, persisted_result),) = stage.persisted
    assert (target, calibration) == (TARGET, CALIBRATION)
    assert persisted_result == (DEPTH_RESULT if kind == "depth" else MEASURE_RESULT)
    assert (result.tenant_id, result.dive_id) == (TENANT, DIVE)


@pytest.mark.parametrize("kind", ["depth", "measure"])
async def test_returns_none_when_selector_finds_no_dive(kind):
    stage = Stage(kind, None, None)

    assert await _run(stage) is None
    assert stage.events == []


@pytest.mark.parametrize("kind", ["depth", "measure"])
async def test_no_work_wakes_nothing_and_dispatches_nothing(kind):
    """The cohort offered the dive but the work was taken in between (the
    cohort and the resolver read the same view, so only a race does this)."""
    stage = Stage(kind, TARGET, _resolution(kind, payload=False))

    result = await _run(stage)

    assert stage.events == []
    assert (result.tenant_id, result.dive_id) == (TENANT, DIVE)


async def test_the_depth_run_returns_its_counters():
    """v1's `ComputeLaserDepthsResult` counters, which the parent now
    returns: a green run is not proof the work was done."""
    stage = Stage("depth", TARGET, _resolution("depth"))

    result = await _run(stage)

    assert result == LaserDepthsRun(
        tenant_id=TENANT,
        dive_id=DIVE,
        computed=1,
        refused=2,
        skipped_current=3,
        skipped_refused=1,
        skipped_unusable_label=2,
        skipped_stale=0,
    )


async def test_the_measure_run_returns_its_counters():
    stage = Stage("measure", TARGET, _resolution("measure"))

    result = await _run(stage)

    assert result == MeasureFishRun(
        tenant_id=TENANT,
        dive_id=DIVE,
        measured=1,
        refused=2,
        skipped_stale=0,
        fish_created=1,
        clusters_bound=1,
        skipped_already_measured=3,
        skipped_unmeasurable_species=4,
        missing_cluster=5,
        missing_laser_or_headtail=6,
        skipped_refused=1,
    )


@pytest.mark.parametrize("kind", ["depth", "measure"])
async def test_a_completed_child_with_the_same_id_does_not_block_the_run(kind):
    """v1's ALLOW_DUPLICATE (dive 59: FAILED_ONLY swallowed every re-dispatch
    of a completed child, so a remediated dive could never be re-measured)."""
    child_input = DEPTH_INPUT if kind == "depth" else MEASURE_INPUT

    async def a_prior_child_completed(client):
        await client.execute_workflow(
            CHILDREN[kind].__temporal_workflow_definition.name,
            child_input,
            id=CHILD_IDS[kind],
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
            result_type=dict,
        )

    stage = Stage(kind, TARGET, _resolution(kind))

    await _run(stage, before=a_prior_child_completed)

    assert stage.events[-2:] == ["child", "persist"]
    assert len(stage.persisted) == 1


async def test_a_running_child_with_the_same_id_is_left_to_persist():
    """Another firing's child is still running (a manual run overlapping the
    schedule): it will persist, so this run does not."""

    async def another_firing_is_running(client):
        await client.start_workflow(
            "ComputeLaserDepthsWorkflow",
            DEPTH_INPUT,
            id=CHILD_IDS["depth"],
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
        )

    stage = Stage("depth", TARGET, _resolution("depth"))

    result = await _run(
        stage, child=HangingDepthChild, before=another_firing_is_running
    )

    assert result.dispatched_elsewhere
    assert stage.persisted == []
