"""The laser parents: stage 0.1, laser prediction, the gate's drain, populate.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/ (test_preprocess_laser_images_parent_workflow.py,
test_predict_laser_images_parent_workflow.py,
test_evaluate_laser_auto_accept_parent_workflow.py,
test_auto_accept_child_dispatch_budget.py,
test_populate_laser_label_studio_project_parent_workflow.py,
test_overlapping_child_is_not_cleaned_up.py, test_reprocess_flag_drains.py).
Names and reasons are v1's. v2 adaptations: the target is (tenant, dive);
the children run on the processor's queues; the wakes stand the processor up.

v2 changes, pinned here:

* the gate is read, run and written in three steps (resolve on the
  orchestrator, judge on the light processor, record on the orchestrator),
  inside v1's unchanged 1 h drain run timeout;
* the stage-0.1 flag clear is scoped by the captures redrawn (v1: their
  checksums -- one canonical capture per checksum per tenant).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List, Optional

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_contracts.laser import (
    GATE_CHILD_EXECUTION_TIMEOUT,
    EvaluateLaserAutoAcceptInput,
    LaserAutoAcceptResult,
    LaserAutoAcceptSummary,
    LaserPredictImage,
    LaserPredictionResult,
    LaserPreprocessImage,
    PredictLaserImagesInput,
    PreprocessLaserImagesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.laser import workflow as sut
from fishsense_services_orchestrator.laser.contracts import (
    ClearReprocessFlags,
    LaserTarget,
)
from fishsense_services_orchestrator.laser.stage import STAGE
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

from ._laser_workflows import (
    Blocker,
    StubGate,
    StubPredict,
    StubPreprocess,
)

QUEUE = "test-laser-parents"
TENANT, DIVE = uuid.UUID(int=1), uuid.UUID(int=10)
TARGET = LaserTarget(tenant_id=TENANT, dive_id=DIVE)
CAPTURES = [uuid.UUID(int=100 + i) for i in range(3)]
K = [[3000.0, 0.0, 2000.0], [0.0, 3000.0, 1500.0], [0.0, 0.0, 1.0]]
RAW = ObjectRef(bucket="scratch", key=f"tenants/{TENANT}/raw/abc.ORF")
JPEG = ObjectRef(bucket="labels", key=f"tenants/{TENANT}/preprocess_jpeg/abc.JPG")


class Script:
    """What the stub activities answer, and the order they were called in."""

    def __init__(self, *, target=TARGET, images=CAPTURES, mode="gpu",
                 predictions=None, auto_accepted=1, populate=()):  # fmt: skip
        self.target = target
        self.images = list(images)
        self.mode = mode
        self.predictions = (
            [LaserPredictionResult(capture_id=c, x=1.0, y=2.0, confidence=0.9,
                                   predictor_version=2) for c in self.images]
            if predictions is None else predictions
        )  # fmt: skip
        self.auto_accepted = auto_accepted
        self.populate = list(populate)
        self.events: list = []
        self.children: list = []
        self.cleared: list = []

    def activities(self):
        s = self

        def record(name):
            s.events.append(name)

        @activity.defn(name="select_next_dive_for_laser_preprocessing")
        async def select_pre() -> Optional[LaserTarget]:
            record("select")
            return s.target

        @activity.defn(name="select_next_dive_for_laser_prediction")
        async def select_predict() -> Optional[LaserTarget]:
            record("select")
            return s.target

        @activity.defn(name="select_next_dive_for_laser_auto_accept")
        async def select_gate() -> Optional[LaserTarget]:
            record("select")
            return s.target

        @activity.defn(name="resolve_laser_preprocess_inputs")
        async def resolve_pre(target: LaserTarget) -> PreprocessLaserImagesInput:
            record("resolve")
            return PreprocessLaserImagesInput(
                dive_id=target.dive_id,
                images=[LaserPreprocessImage(capture_id=c, raw=RAW, jpeg=JPEG)
                        for c in s.images],
                camera_matrix=K, distortion_coefficients=[0.0] * 5,
                bbox=[1580, 395, 2470, 1905],
            )  # fmt: skip

        @activity.defn(name="resolve_laser_predict_inputs")
        async def resolve_predict(target: LaserTarget) -> PredictLaserImagesInput:
            record("resolve")
            return PredictLaserImagesInput(
                dive_id=target.dive_id,
                images=[LaserPredictImage(capture_id=c, raw=RAW) for c in s.images],
                camera_matrix=K, distortion_coefficients=[0.0] * 5,
            )  # fmt: skip

        @activity.defn(name="clear_laser_reprocess_flags")
        async def clear(request: ClearReprocessFlags) -> int:
            record("clear")
            s.cleared.append(request.capture_ids)
            return 1

        @activity.defn(name="ensure_per_image_processor_running")
        async def wake_per_image() -> None:
            record("wake-per-image")

        @activity.defn(name="ensure_gpu_processor_running")
        async def wake_gpu() -> str:
            record("wake-gpu")
            return s.mode

        @activity.defn(name="ensure_light_processor_running")
        async def wake_light() -> None:
            record("wake-light")

        @activity.defn(name="stage_raw_bytes_for_dive")
        async def stage(target: StagingTarget) -> StageRawBytesResult:
            record("stage")
            return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

        @activity.defn(name="cleanup_raw_bytes_for_dive")
        async def cleanup(target: StagingTarget) -> CleanupRawBytesResult:
            record("cleanup")
            return CleanupRawBytesResult(deleted=1)

        @activity.defn(name="persist_laser_predictions")
        async def persist(
            target: LaserTarget, results: List[LaserPredictionResult]
        ) -> int:
            record("persist")
            return len(results)

        @activity.defn(name="backfill_laser_predictions_for_dive")
        async def backfill(target: LaserTarget) -> int:
            record("backfill")
            return 0

        @activity.defn(name="resolve_laser_gate_inputs")
        async def resolve_gate(target: LaserTarget) -> EvaluateLaserAutoAcceptInput:
            record("resolve-gate")
            return EvaluateLaserAutoAcceptInput(
                dive_id=target.dive_id, dive_number=7, predictions=[]
            )

        @activity.defn(name="record_laser_gate_verdicts")
        async def record_verdicts(
            target: LaserTarget, result: LaserAutoAcceptResult
        ) -> LaserAutoAcceptSummary:
            record("record")
            return result.summary

        @activity.defn(name="apply_laser_auto_accept_for_dive")
        async def apply(target: LaserTarget) -> int:
            record("apply")
            return 1

        @activity.defn(name="select_dives_needing_laser_population")
        async def select_populate() -> List[LaserTarget]:
            return s.populate

        @activity.defn(name="create_laser_label_studio_project")
        async def create(target: LaserTarget) -> int:
            record(f"create:{target.dive_id.int}")
            return 500 + target.dive_id.int

        @activity.defn(name="populate_laser_label_studio_project")
        async def populate(target: LaserTarget, project_id: int) -> int:
            record(f"populate:{target.dive_id.int}:{project_id}")
            if target.dive_id.int == 666:
                raise RuntimeError("simulated populate failure")
            return 1

        @activity.defn(name="_child")
        async def child(kind: str, workflow_id: str) -> None:
            record("child")
            s.children.append((kind, workflow_id, activity.info().task_queue))

        @activity.defn(name="_predictions")
        async def predictions(
            workflow_id: str, payload: PredictLaserImagesInput
        ) -> List[LaserPredictionResult]:
            record("child")
            s.children.append(("predict", workflow_id, activity.info().task_queue))
            return s.predictions

        @activity.defn(name="_gate")
        async def gate(workflow_id: str, payload: EvaluateLaserAutoAcceptInput):
            record("gate")
            s.children.append(("gate", workflow_id, activity.info().task_queue))
            return LaserAutoAcceptResult(
                summary=LaserAutoAcceptSummary(
                    dive_id=payload.dive_id, eligible=bool(s.auto_accepted),
                    auto_accepted=s.auto_accepted,
                    verdicts={"auto_accepted": s.auto_accepted},
                ),
                frames=[],
            )  # fmt: skip

        orchestrator = [
            select_pre, select_predict, select_gate, resolve_pre, resolve_predict,
            clear, wake_per_image, wake_gpu, wake_light, stage, cleanup, persist,
            backfill, resolve_gate, record_verdicts, apply, select_populate,
            create, populate,
        ]  # fmt: skip
        return orchestrator, [child], [predictions], [gate]


async def _run(workflow_run, script: Script, *, before=None, arg=None):
    orchestrator, per_image, gpu, light = script.activities()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(env.client, task_queue=QUEUE,
                   workflows=[*STAGE.workflows, Blocker], activities=orchestrator),
            Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE,
                   workflows=[StubPreprocess], activities=per_image),
            Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                   workflows=[StubPredict], activities=gpu),
            Worker(env.client, task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                   workflows=[StubGate], activities=light),
        ):  # fmt: skip
            blockers = []
            if before:
                for workflow_id in before:
                    blockers.append(
                        await env.client.start_workflow(
                            Blocker.run, id=workflow_id, task_queue=QUEUE
                        )
                    )
            args = () if arg is None else (arg,)
            result = await env.client.execute_workflow(
                workflow_run, *args, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )
            # The blockers are left to the environment: time skipping runs
            # them out once the parent is done.
            assert all(handle.id for handle in blockers)
            return result


# -- stage 0.1 ------------------------------------------------------------------


async def test_dispatches_child_with_deterministic_id_and_correct_payload():
    script = Script()

    result = await _run(sut.PreprocessLaserImagesParentWorkflow.run, script)

    assert result == TARGET
    assert script.children == [
        ("preprocess", f"preprocess-laser-{DIVE}", PROCESSOR_TASK_QUEUE)
    ]
    assert script.events == [
        "select", "resolve", "wake-per-image", "stage", "child", "cleanup", "clear"]  # fmt: skip


async def test_the_flag_clear_is_scoped_to_what_was_redrawn():
    """A flag raised while the child ran (up to 2 h) must survive."""
    script = Script()

    await _run(sut.PreprocessLaserImagesParentWorkflow.run, script)

    assert script.cleared == [CAPTURES]


async def test_returns_none_when_selector_finds_no_dive():
    script = Script(target=None)

    assert await _run(sut.PreprocessLaserImagesParentWorkflow.run, script) is None
    assert script.events == ["select"]


async def test_no_work_lowers_the_whole_dives_flags_and_wakes_nothing():
    """The flag is the one cohort term that never goes false by itself: dive
    60 blocked 84/465/471 until it was lowered."""
    script = Script(images=[])

    await _run(sut.PreprocessLaserImagesParentWorkflow.run, script)

    assert script.events == ["select", "resolve", "clear"]
    assert script.cleared == [None]


async def test_a_running_child_owns_the_scratch_and_the_flags():
    """Prod dive 442 lost 984 raw objects and 515 flags to a firing that
    cleaned up under another's child."""
    script = Script()

    await _run(
        sut.PreprocessLaserImagesParentWorkflow.run,
        script,
        before=[f"preprocess-laser-{DIVE}"],
    )

    assert "cleanup" not in script.events and "clear" not in script.events


# -- laser prediction -------------------------------------------------------------


async def test_full_path_dispatches_child_persists_and_gates():
    script = Script()

    result = await _run(sut.PredictLaserImagesParentWorkflow.run, script)

    assert result == TARGET
    assert script.events == [
        "select", "resolve", "wake-gpu", "stage", "child", "persist", "cleanup",
        "backfill", "wake-light", "resolve-gate", "gate", "record", "apply",
    ]  # fmt: skip
    assert script.children == [
        ("predict", f"predict-laser-{DIVE}", PROCESSOR_GPU_TASK_QUEUE),
        ("gate", f"auto-accept-laser-{DIVE}", PROCESSOR_LIGHT_TASK_QUEUE),
    ]


async def test_selector_none_returns_none():
    script = Script(target=None)

    assert await _run(sut.PredictLaserImagesParentWorkflow.run, script) is None
    assert script.events == ["select"]


async def test_no_images_skips_child_and_persist():
    script = Script(images=[])

    await _run(sut.PredictLaserImagesParentWorkflow.run, script)

    assert script.events == ["select", "resolve"]


async def test_cpu_fallback_capacity_still_dispatches():
    script = Script(mode="cpu_fallback")

    await _run(sut.PredictLaserImagesParentWorkflow.run, script)

    assert "child" in script.events


async def test_unavailable_capacity_bails_before_staging_anything():
    """A child on an unserved queue hangs until its 6 h timeout."""
    script = Script(mode="unavailable")

    assert await _run(sut.PredictLaserImagesParentWorkflow.run, script) is None
    assert script.events == ["select", "resolve", "wake-gpu"]


async def test_backfill_is_skipped_when_the_child_returned_nothing():
    """And cleanup still runs: the scratch exists for the child, now done."""
    script = Script(predictions=[])

    await _run(sut.PredictLaserImagesParentWorkflow.run, script)

    assert script.events == [
        "select", "resolve", "wake-gpu", "stage", "child", "cleanup"]  # fmt: skip


async def test_nothing_is_applied_when_the_gate_cleared_nothing():
    script = Script(auto_accepted=0)

    await _run(sut.PredictLaserImagesParentWorkflow.run, script)

    assert "record" in script.events and "apply" not in script.events


async def test_a_running_predict_child_owns_the_scratch():
    script = Script()

    await _run(
        sut.PredictLaserImagesParentWorkflow.run,
        script,
        before=[f"predict-laser-{DIVE}"],
    )

    assert script.events == ["select", "resolve", "wake-gpu", "stage"]


async def test_a_running_gate_is_left_to_the_other_parent():
    """The drain and the predict parent share `auto-accept-laser-{dive}`;
    reading `.eligible` off nothing would wedge the workflow task (v1)."""
    script = Script()

    await _run(
        sut.PredictLaserImagesParentWorkflow.run,
        script,
        before=[f"auto-accept-laser-{DIVE}"],
    )

    assert "record" not in script.events and "apply" not in script.events


# -- the gate's backlog drain -------------------------------------------------------


async def test_empty_backlog_returns_none_and_touches_nothing():
    script = Script(target=None)

    assert await _run(sut.EvaluateLaserAutoAcceptParentWorkflow.run, script) is None
    assert script.events == ["select"]


async def test_selected_dive_is_judged_then_applied():
    script = Script()

    await _run(sut.EvaluateLaserAutoAcceptParentWorkflow.run, script)

    assert script.events == [
        "select", "wake-light", "resolve-gate", "gate", "record", "apply"]  # fmt: skip


async def test_a_refused_dive_is_judged_but_not_applied():
    script = Script(auto_accepted=0)

    await _run(sut.EvaluateLaserAutoAcceptParentWorkflow.run, script)

    assert "record" in script.events and "apply" not in script.events


async def test_child_id_matches_the_predict_parent_so_a_dive_is_never_judged_twice():
    drain, predict = Script(), Script()

    await _run(sut.EvaluateLaserAutoAcceptParentWorkflow.run, drain)
    await _run(sut.PredictLaserImagesParentWorkflow.run, predict)

    gate_ids = {
        workflow_id
        for script in (drain, predict)
        for kind, workflow_id, queue in script.children
        if kind == "gate" and queue == PROCESSOR_LIGHT_TASK_QUEUE
    }
    assert gate_ids == {f"auto-accept-laser-{DIVE}"}


def test_the_whole_drain_fits_inside_v1s_deployed_run_timeout():
    """v1's drain schedule runs for at most 1 h, and a schedule is never
    updated in place. Worst case -- every step burning its budget -- must fit:
    select, the gate's read, the wake, the child, the gate's write, the apply."""
    from fishsense_services_orchestrator.nrp.workflow import WAKE_TIMEOUT

    worst = (
        sut.SELECT_TIMEOUT
        + sut.GATE_READ_TIMEOUT
        + WAKE_TIMEOUT  # wake_light_processor
        + GATE_CHILD_EXECUTION_TIMEOUT
        + sut.GATE_WRITE_TIMEOUT
        + sut.LABEL_STUDIO_TIMEOUT
    )
    (drain,) = [
        s for s in STAGE.schedules if s.schedule_id == "evaluate-laser-auto-accept"
    ]

    assert worst <= drain.run_timeout == timedelta(hours=1)


# -- populate ---------------------------------------------------------------------


async def test_fans_out_populate_child_per_dive_and_survives_one_failing():
    dives = [
        LaserTarget(tenant_id=TENANT, dive_id=uuid.UUID(int=n)) for n in (5, 666, 7)
    ]
    script = Script(populate=dives)

    result = await _run(sut.PopulateLaserLabelStudioProjectParentWorkflow.run, script)

    assert [t.dive_id.int for t in result] == [5, 666, 7]
    for n in (5, 7):
        assert f"create:{n}" in script.events
        assert f"populate:{n}:{500 + n}" in script.events


async def test_no_dispatch_when_cohort_empty():
    script = Script(populate=[])

    assert (
        await _run(sut.PopulateLaserLabelStudioProjectParentWorkflow.run, script) == []
    )
    assert script.events == []


async def test_create_then_populate_the_one_dive():
    script = Script()

    written = await _run(
        sut.PopulateLaserLabelStudioProjectWorkflow.run, script, arg=TARGET
    )

    assert written == 1
    assert script.events == [
        f"create:{DIVE.int}",
        f"populate:{DIVE.int}:{500 + DIVE.int}",
    ]


# -- schedules ------------------------------------------------------------------------


def test_v1s_schedules_keep_v1s_minutes_overlap_and_run_timeouts():
    schedules = {s.schedule_id: s for s in STAGE.schedules}

    assert {
        sid: (s.every, s.offset, s.run_timeout, s.overlap.name)
        for sid, s in schedules.items()
    } == {
        "preprocess-laser-images": (timedelta(hours=1), timedelta(0),
                                    timedelta(hours=1), "SKIP"),
        "predict-laser-images": (timedelta(hours=1), timedelta(minutes=10),
                                 timedelta(hours=2), "SKIP"),
        "populate-laser-labels": (timedelta(hours=1), timedelta(minutes=12),
                                  timedelta(hours=1), "SKIP"),
        "evaluate-laser-auto-accept": (timedelta(hours=1), timedelta(minutes=22),
                                       timedelta(hours=1), "SKIP"),
    }  # fmt: skip
