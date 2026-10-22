"""The shadow calibration's workflows (docs/shadow-size-constancy.md §3).

Per mission: resolve; nothing to segment is nothing woken; the GPU is woken
and **only a real GPU segments** (the CPU fallback, like unavailable, skips
the mission and stops the parent's drain); SAM children chunked under the
payload limit; the fit on the light queue; the results stored. The parent
drains the cohort, never picking a mission twice in a run.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List, Optional

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
)
from fishsense_services_contracts.shadow_calibration import (
    FitShadowCalibrationInput,
    SegmentSlateFrame,
    SegmentSlateFramesInput,
    ShadowFitResult,
    SlateSegment,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.shadow_calibration import workflow as sut
from fishsense_services_orchestrator.shadow_calibration.activities import (
    ShadowPlan,
    ShadowTarget,
)
from fishsense_services_orchestrator.shadow_calibration.workflow import (
    ShadowCalibrationForMissionWorkflow,
    ShadowCalibrationParentWorkflow,
)

TENANT, MISSION = uuid.uuid4(), uuid.uuid4()
TARGET = ShadowTarget(TENANT, MISSION)
QUEUE = "test-shadow-calibration"
EVENTS: list[tuple] = []
SELECTS: list[tuple] = []


def _plan(n, unrendered=0):
    frames = [SegmentSlateFrame(capture_id=uuid.uuid4(), render_url="https://g/r?sig",
                                dots=[[1.0, 2.0]]) for _ in range(n)]  # fmt: skip
    return ShadowPlan(
        unrendered=unrendered,
        segment=SegmentSlateFramesInput(tenant_id=TENANT, mission_id=MISSION, frames=frames),
        fit=FitShadowCalibrationInput(tenant_id=TENANT, mission_id=MISSION,
                                      mission_number=7, camera_matrix=[[1.0]],
                                      frames={}, sam_model=""),
    )  # fmt: skip


def _stubs(*, mode="gpu", frames=2, selected=TARGET, unrendered=0, broken=()):
    """`broken`: missions whose resolve fails (a camera that isn't pinhole, a
    tenant with no bucket)."""
    cohort = list(selected) if isinstance(selected, list) else None

    @activity.defn(name="select_next_mission_for_shadow_calibration")
    async def select(
        exclude: Optional[List[uuid.UUID]] = None,
    ) -> Optional[ShadowTarget]:
        SELECTS.append(tuple(exclude or []))
        if cohort is None:
            return selected
        return cohort.pop(0) if cohort else None

    @activity.defn(name="resolve_shadow_calibration_inputs")
    async def resolve(t: ShadowTarget) -> ShadowPlan:
        EVENTS.append(("resolve", t.mission_id))
        if t.mission_id in broken:
            raise ApplicationError("not pinhole", type="ValueError", non_retryable=True)
        return _plan(frames, unrendered)

    @activity.defn(name="assemble_shadow_fit")
    async def assemble(
        p: ShadowPlan, s: List[SlateSegment]
    ) -> FitShadowCalibrationInput:
        EVENTS.append(("assemble", len(s)))
        return p.fit

    @activity.defn(name="persist_shadow_calibration")
    async def persist(t: ShadowTarget, r: ShadowFitResult) -> int:
        EVENTS.append(("persist", len(r.calibrations)))
        return len(r.calibrations)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake_gpu() -> str:
        EVENTS.append(("wake gpu",))
        return mode

    @activity.defn(name="ensure_light_processor_running")
    async def wake_light() -> None:
        EVENTS.append(("wake light",))

    return [select, resolve, assemble, persist, wake_gpu, wake_light]


@activity.defn(name="_record")
async def _record(event: List[str]) -> None:
    EVENTS.append(tuple(event))


async def _record_child(n: int) -> None:
    await workflow.execute_activity(
        "_record", ["child", workflow.info().workflow_id, str(n)],
        schedule_to_close_timeout=timedelta(seconds=5),
    )  # fmt: skip


@workflow.defn(name="SegmentSlateFramesWorkflow")
class _Segment:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        await _record_child(len(payload["frames"]))
        return [SlateSegment(capture_id=f["capture_id"], status="segmented").model_dump(
            mode="json") for f in payload["frames"]]  # fmt: skip


@workflow.defn(name="FitShadowCalibrationWorkflow")
class _Fit:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> dict:
        await _record_child(0)
        return ShadowFitResult(mission_id=payload["dive_id"], calibrations=[]).model_dump(
            mode="json")  # fmt: skip


async def _run(workflow_cls, *args, **stubs):
    EVENTS.clear()
    SELECTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        workers = [
            Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                   workflows=[ShadowCalibrationForMissionWorkflow,
                              ShadowCalibrationParentWorkflow]),
            Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE, activities=[_record],
                   workflows=[_Segment]),
            Worker(env.client, task_queue=PROCESSOR_LIGHT_TASK_QUEUE, activities=[_record],
                   workflows=[_Fit]),
        ]  # fmt: skip
        async with workers[0], workers[1], workers[2]:
            return await env.client.execute_workflow(
                workflow_cls.run, *args, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


async def test_one_mission_every_step_in_order():
    assert await _run(ShadowCalibrationForMissionWorkflow, TARGET) == "stored"
    assert EVENTS == [
        ("resolve", MISSION),
        ("wake gpu",),
        ("child", f"shadow-segment-{MISSION}-0", "2"),
        ("assemble", 2),
        ("wake light",),
        ("child", f"shadow-fit-{MISSION}", "0"),
        ("persist", 0),
    ]


async def test_segments_in_chunks_under_the_payload_limit():
    n = 2 * sut.SEGMENT_CHUNK + 1
    await _run(ShadowCalibrationForMissionWorkflow, TARGET, frames=n)
    children = sorted(e for e in EVENTS if e[0] == "child" and "segment" in e[1])
    sizes = [str(sut.SEGMENT_CHUNK), str(sut.SEGMENT_CHUNK), "1"]
    assert children == [("child", f"shadow-segment-{MISSION}-{i}", k)
                        for i, k in enumerate(sizes)]  # fmt: skip
    assert ("assemble", n) in EVENTS


async def test_nothing_to_segment_is_still_fitted_and_stored_without_a_gpu():
    """Review of #261: a mission with no dot is stored (as refused) so the
    cohort moves past it; it never wakes the GPU."""
    assert await _run(ShadowCalibrationForMissionWorkflow, TARGET, frames=0) == "stored"
    assert EVENTS == [
        ("resolve", MISSION),
        ("assemble", 0),
        ("wake light",),
        ("child", f"shadow-fit-{MISSION}", "0"),
        ("persist", 0),
    ]


async def test_a_mission_with_unrendered_slate_frames_waits_and_stores_nothing():
    """Review of #261: fitted on part of its frames, the stored result would
    be final; the mission waits for its renders instead."""
    outcome = await _run(ShadowCalibrationForMissionWorkflow, TARGET, unrendered=3)
    assert outcome == "renders_pending"
    assert EVENTS == [("resolve", MISSION)]


async def test_only_a_real_gpu_segments():
    for mode in ("cpu_fallback", "unavailable"):
        assert (
            await _run(ShadowCalibrationForMissionWorkflow, TARGET, mode=mode)
            == "no_gpu"
        )
        assert EVENTS == [("resolve", MISSION), ("wake gpu",)]


async def test_parent_drains_the_cohort_and_never_repeats_a_mission():
    other = ShadowTarget(TENANT, uuid.uuid4())
    done = await _run(ShadowCalibrationParentWorkflow, selected=[TARGET, other])
    assert done == [f"{MISSION}:stored", f"{other.mission_id}:stored"]
    assert SELECTS == [(), (MISSION,), (MISSION, other.mission_id)]


async def test_parent_stops_when_no_gpu():
    other = ShadowTarget(TENANT, uuid.uuid4())
    done = await _run(ShadowCalibrationParentWorkflow, selected=[TARGET, other],
                      mode="unavailable")  # fmt: skip
    assert done == [f"{MISSION}:no_gpu"]


async def test_one_failing_mission_does_not_stop_the_drain():
    """Review of #261: the parent goes on past a mission that fails."""
    bad, good = ShadowTarget(TENANT, uuid.uuid4()), ShadowTarget(TENANT, uuid.uuid4())
    done = await _run(ShadowCalibrationParentWorkflow, selected=[bad, good],
                      broken=(bad.mission_id,))  # fmt: skip
    assert done == [f"{bad.mission_id}:failed", f"{good.mission_id}:stored"]


async def test_a_pending_mission_does_not_stop_the_drain():
    other = ShadowTarget(TENANT, uuid.uuid4())
    done = await _run(ShadowCalibrationParentWorkflow, selected=[TARGET, other],
                      unrendered=1)  # fmt: skip
    assert done == [f"{MISSION}:renders_pending", f"{other.mission_id}:renders_pending"]
