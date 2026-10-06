"""The slate detector's parent workflow.

New in v2, shaped as laser prediction's parent is (the raw-reading GPU
stage), and pinned the same way:

* select, resolve, wake the GPU processor, stage the raws, the child on the
  processor's GPU queue under `raw_scratch_reader_id("detect-slate", dive)`
  (reused whatever the last run did), persist, then clean the scratch up;
* nothing to detect needs no worker and stages nothing;
* `unavailable` from the GPU wake means no staging and no child (an unserved
  queue hangs);
* a child still running means do nothing more: it owns the scratch;
* one run drains the backlog: it takes the next dive until the drain window
  has passed, the cohort is empty, it hands back a dive this run already
  took, or the GPU is unavailable;
* the run timeout covers the window and then every step at its longest.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import List

from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_presence import (
    DetectSlateImage,
    DetectSlateImagesInput,
    SlatePresenceResult,
    SlateRender,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.readers import RAW_SCRATCH_READERS
from fishsense_services_orchestrator.slate_detect import workflow as sut
from fishsense_services_orchestrator.slate_detect.workflow import (
    DetectSlatePresenceParentWorkflow,
)

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = StagingTarget(TENANT, DIVE)
QUEUE = "test-slate-detect-parent"
K = [[3500.0, 0.0, 2000.0], [0.0, 3500.0, 1500.0], [0.0, 0.0, 1.0]]
SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"

EVENTS: list[tuple] = []
CHILD_RESULTS: list[SlatePresenceResult] = []
#: How long the stub child takes (skipped by the time-skipping server). Read
#: through an activity: the sandboxed child sees its own copy of this module.
CHILD_TAKES: list[timedelta] = [timedelta(0)]


def _inputs(n=1):
    return DetectSlateImagesInput(
        tenant_id=TENANT,
        dive_id=DIVE,
        camera_matrix=K,
        distortion_coefficients=[0.0] * 5,
        images=[
            DetectSlateImage(
                capture_id=uuid.uuid4(),
                raw=ObjectRef(
                    bucket="scratch", key=f"tenants/{TENANT}/raw/{i:032x}.ORF"
                ),
            )
            for i in range(n)
        ],
    )


def _result():
    return SlatePresenceResult(
        capture_id=uuid.uuid4(), status="predicted", probability=0.9,
        model_version=1, weights_sha256=SHA,
        render=SlateRender(decode_config="production", decode_params={}),
        predicted_at=datetime(2026, 10, 5, tzinfo=UTC),
    )  # fmt: skip


@activity.defn(name="_record")
async def _record(event: List[str]) -> None:
    EVENTS.append(tuple(event))


@activity.defn(name="_child_results")
async def _child_results() -> List[SlatePresenceResult]:
    return list(CHILD_RESULTS)


@activity.defn(name="_child_takes")
async def _child_takes() -> float:
    return CHILD_TAKES[0].total_seconds()


@workflow.defn(name="DetectSlatePresenceWorkflow")
class _StubChild:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: DetectSlateImagesInput) -> List[SlatePresenceResult]:
        await workflow.execute_activity(
            "_record",
            ["child", workflow.info().workflow_id],
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        takes = await workflow.execute_activity(
            "_child_takes", schedule_to_close_timeout=timedelta(seconds=5)
        )
        await workflow.sleep(takes)
        return await workflow.execute_activity(
            "_child_results",
            schedule_to_close_timeout=timedelta(seconds=5),
            result_type=List[SlatePresenceResult],
        )


def _stubs(*, mode="gpu", images=1, selected=TARGET):
    """`selected` is the cohort's answer every time, or a list of answers in
    turn (None once it runs out); `mode` likewise for the GPU wake."""
    cohort = list(selected) if isinstance(selected, list) else None
    modes = list(mode) if isinstance(mode, list) else None

    @activity.defn(name="select_next_dive_for_slate_detection")
    async def select() -> StagingTarget | None:
        if cohort is None:
            return selected
        return cohort.pop(0) if cohort else None

    @activity.defn(name="resolve_slate_detection_inputs")
    async def resolve(target: StagingTarget) -> DetectSlateImagesInput:
        return _inputs(images)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return modes.pop(0) if modes is not None else mode

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(target: StagingTarget) -> StageRawBytesResult:
        EVENTS.append(("stage", target.dive_id))
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup(target: StagingTarget) -> CleanupRawBytesResult:
        EVENTS.append(("cleanup", target.dive_id))
        return CleanupRawBytesResult(deleted=1)

    @activity.defn(name="persist_slate_presence_predictions")
    async def persist(target: StagingTarget, results: List[SlatePresenceResult]) -> int:
        EVENTS.append(("persist", len(results)))
        return len(results)

    return [select, resolve, wake, stage, cleanup, persist]


async def _run(child_results=(), before=None, child_takes=timedelta(0), **stubs):
    EVENTS.clear()
    CHILD_RESULTS.clear()
    CHILD_RESULTS.extend(child_results)
    CHILD_TAKES[0] = child_takes
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DetectSlatePresenceParentWorkflow],
                activities=_stubs(**stubs) + [_record, _child_results, _child_takes],
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                workflows=[_StubChild],
                activities=[_record, _child_results, _child_takes],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                DetectSlatePresenceParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


def _kinds():
    return [e[0] for e in EVENTS]


async def test_a_normal_run_stages_detects_persists_and_cleans_up():
    assert await _run([_result(), _result()]) == TARGET
    assert EVENTS == [
        ("wake",),
        ("stage", DIVE),
        ("child", f"detect-slate-{DIVE}"),
        ("persist", 2),
        ("cleanup", DIVE),
    ]


def test_the_child_is_a_raw_scratch_reader():
    """Or another stage's cleanup deletes the frames under it."""
    assert "detect-slate" in RAW_SCRATCH_READERS


async def test_the_cpu_fallback_serves_the_same_queue():
    assert await _run([_result()], mode="cpu_fallback") == TARGET
    assert ("persist", 1) in EVENTS


async def test_no_results_persists_nothing_but_still_cleans_up():
    await _run([])
    assert "persist" not in _kinds()
    assert ("cleanup", DIVE) in EVENTS


async def test_no_worker_available_means_no_staging_and_no_child():
    assert await _run(mode="unavailable") is None
    assert _kinds() == ["wake"]


async def test_no_images_needs_no_worker():
    assert await _run(images=0) == TARGET
    assert not EVENTS


async def test_no_dive_is_none():
    assert await _run(selected=None) is None
    assert not EVENTS


async def test_a_child_already_running_means_do_nothing_more():
    """It owns the scratch, and it persists."""

    async def a_child_is_running(client):
        await client.start_workflow(
            "DetectSlatePresenceWorkflow",
            _inputs(),
            id=f"detect-slate-{DIVE}",
            task_queue="nobody-polls-this",
        )

    assert await _run(before=a_child_is_running) == TARGET
    assert _kinds() == ["wake", "stage"]


def _dive():
    return StagingTarget(TENANT, uuid.uuid4())


def _children():
    return [e[1] for e in EVENTS if e[0] == "child"]


async def test_one_run_drains_the_backlog_until_the_cohort_is_empty():
    dives = [_dive(), _dive(), _dive()]
    assert await _run([_result()], selected=dives) == dives[-1]
    assert _children() == [f"detect-slate-{d.dive_id}" for d in dives]
    assert [e for e in EVENTS if e[0] == "cleanup"] == [
        ("cleanup", d.dive_id) for d in dives
    ]


async def test_no_new_dive_is_started_once_the_drain_window_has_passed():
    """The window bounds when a dive may start, not how long it may run."""
    dives = [_dive() for _ in range(5)]
    most = sut.DETECT_DRAIN_WINDOW * 0.6
    assert await _run([_result()], selected=dives, child_takes=most) == dives[1]
    assert _children() == [f"detect-slate-{d.dive_id}" for d in dives[:2]]


async def test_a_dive_handed_back_twice_ends_the_run():
    """A dive whose frames all fail to persist stays in the cohort; taking it
    again would spin until the window closed."""
    assert await _run([_result()]) == TARGET
    assert _children() == [f"detect-slate-{DIVE}"]


async def test_the_gpu_going_away_mid_drain_ends_the_run():
    dives = [_dive(), _dive(), _dive()]
    got = await _run([_result()], selected=dives, mode=["gpu", "unavailable"])
    assert got == dives[0]
    assert _children() == [f"detect-slate-{dives[0].dive_id}"]
    assert ("stage", dives[1].dive_id) not in EVENTS


async def test_a_dive_with_nothing_to_detect_does_not_end_the_drain():
    dives = [_dive(), _dive()]
    assert await _run(images=0, selected=dives) == dives[-1]
    assert not EVENTS


def test_the_run_outlives_every_step_it_waits_on():
    assert sut.DETECT_RUN_TIMEOUT >= (
        sut.DETECT_DRAIN_WINDOW
        + sut.DETECT_SELECT_TIMEOUT
        + sut.DETECT_RESOLVE_TIMEOUT
        + sut.GPU_WAKE_TIMEOUT
        + sut.STAGE_RAW_TIMEOUT
        + sut.DETECT_CHILD_TIMEOUT
        + sut.DETECT_PERSIST_TIMEOUT
        + sut.CLEANUP_RAW_TIMEOUT
    )
