"""The species pre-annotation parent and backfill workflows.

New in v2 (no v1 counterpart), shaped as head/tail prediction's parent is
(test_headtail_workflows.py), and pinned the same way:

* select, resolve, wake the GPU processor, the child on the processor's GPU
  queue under a deterministic id (reused whatever the last run did), persist,
  then the backfill, **unconditionally** (it is also what makes suggestions
  visible, and it attaches nothing while the stage is disabled);
* `unavailable` from the GPU wake means no child (an unserved queue hangs);
* nothing to predict needs no worker;
* a skip (`skipped_no_upgrade_available`) is never persisted;
* a child still running means do nothing more.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List

from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species_prediction import (
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
)
from fishsense_services_orchestrator.species_predict import workflow as sut
from fishsense_services_orchestrator.species_predict.activities import (
    SpeciesPredictTarget,
)
from fishsense_services_orchestrator.species_predict.workflow import (
    BackfillSpeciesPredictionsWorkflow,
    PredictSpeciesImagesParentWorkflow,
)

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = SpeciesPredictTarget(TENANT, DIVE)
QUEUE = "test-species-predict-parent"

EVENTS: list[tuple] = []
CHILD_RESULTS: list[SpeciesPredictionResult] = []


def _inputs(n=1):
    return PredictSpeciesImagesInput(
        tenant_id=TENANT,
        dive_id=DIVE,
        candidates=[SpeciesCandidate(choice="Fish, A (A a)", scientific_name="A a")],
        images=[
            PredictSpeciesImage(
                capture_id=uuid.uuid4(),
                headtail_prediction_id=uuid.uuid4(),
                jpeg=ObjectRef(
                    bucket="labels",
                    key=f"tenants/{TENANT}/preprocess_headtail_jpeg/{i:032x}.JPG",
                ),
                mask_bbox=[1, 2, 3, 4],
            )
            for i in range(n)
        ],
    )


@activity.defn(name="_record")
async def _record(event: List[str]) -> None:
    EVENTS.append(tuple(event))


@activity.defn(name="_child_results")
async def _child_results() -> List[SpeciesPredictionResult]:
    return list(CHILD_RESULTS)


@workflow.defn(name="PredictSpeciesImagesWorkflow")
class _StubChild:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictSpeciesImagesInput
    ) -> List[SpeciesPredictionResult]:
        await workflow.execute_activity(
            "_record",
            ["child", workflow.info().workflow_id],
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return await workflow.execute_activity(
            "_child_results",
            schedule_to_close_timeout=timedelta(seconds=5),
            result_type=List[SpeciesPredictionResult],
        )


def _stubs(*, mode="gpu", images=1, selected=TARGET):
    @activity.defn(name="select_next_dive_for_species_prediction")
    async def select() -> SpeciesPredictTarget | None:
        return selected

    @activity.defn(name="resolve_species_predict_inputs")
    async def resolve(target: SpeciesPredictTarget) -> PredictSpeciesImagesInput:
        return _inputs(images)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return mode

    @activity.defn(name="persist_species_predictions")
    async def persist(
        target: SpeciesPredictTarget, results: List[SpeciesPredictionResult]
    ) -> int:
        EVENTS.append(("persist", [r.status for r in results]))
        return len(results)

    @activity.defn(name="backfill_species_predictions_for_dive")
    async def backfill(target: SpeciesPredictTarget) -> int:
        EVENTS.append(("backfill", target.dive_id))
        return 0

    return [select, resolve, wake, persist, backfill, _record, _child_results]


async def _run(child_results=(), before=None, **stubs):
    EVENTS.clear()
    CHILD_RESULTS.clear()
    CHILD_RESULTS.extend(child_results)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PredictSpeciesImagesParentWorkflow],
                activities=_stubs(**stubs),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                workflows=[_StubChild],
                activities=[_record, _child_results],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                PredictSpeciesImagesParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


def _result(status="predicted"):
    return SpeciesPredictionResult(
        capture_id=uuid.uuid4(), headtail_prediction_id=uuid.uuid4(), status=status,
        predictor_version=1, model_id="bioclip/2.5-vith14@x",
    )  # fmt: skip


def _kinds():
    return [e[0] for e in EVENTS]


async def test_persists_and_backfills_a_normal_run():
    assert await _run([_result()]) == TARGET
    assert EVENTS == [
        ("wake",),
        ("child", f"predict-species-{DIVE}"),
        ("persist", ["predicted"]),
        ("backfill", DIVE),
    ]


async def test_skips_are_never_persisted():
    await _run([_result(), _result(SPECIES_STATUS_NO_UPGRADE_AVAILABLE)])
    assert ("persist", ["predicted"]) in EVENTS


async def test_an_all_skip_run_still_backfills():
    await _run([_result(SPECIES_STATUS_NO_UPGRADE_AVAILABLE)])
    assert "persist" not in _kinds()
    assert ("backfill", DIVE) in EVENTS


async def test_no_worker_available_means_no_child():
    assert await _run(mode="unavailable") is None
    assert _kinds() == ["wake"]


async def test_the_cpu_fallback_serves_the_same_queue():
    assert await _run([_result()], mode="cpu_fallback") == TARGET
    assert ("backfill", DIVE) in EVENTS


async def test_no_images_needs_no_worker():
    assert await _run(images=0) == TARGET
    assert not EVENTS


async def test_no_dive_is_none():
    assert await _run(selected=None) is None
    assert not EVENTS


async def test_a_child_already_running_means_do_nothing_more():
    async def a_child_is_running(client):
        await client.start_workflow(
            "PredictSpeciesImagesWorkflow",
            _inputs(),
            id=f"predict-species-{DIVE}",
            task_queue="nobody-polls-this",
        )

    assert await _run(before=a_child_is_running) == TARGET
    assert _kinds() == ["wake"]


def test_the_run_outlives_every_step_it_waits_on():
    assert sut.PREDICT_RUN_TIMEOUT >= (
        sut.PREDICT_SELECT_TIMEOUT
        + sut.PREDICT_RESOLVE_TIMEOUT
        + sut.GPU_WAKE_TIMEOUT
        + sut.PREDICT_CHILD_TIMEOUT
        + sut.PREDICT_PERSIST_TIMEOUT
        + sut.PREDICT_BACKFILL_TIMEOUT
    )


async def test_the_backfill_workflow_runs_the_dives_backfill():
    @activity.defn(name="backfill_species_predictions_for_dive")
    async def backfill(target: SpeciesPredictTarget) -> int:
        EVENTS.append(("backfill", target))
        return 3

    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[BackfillSpeciesPredictionsWorkflow],
            activities=[backfill],
        ):
            attached = await env.client.execute_workflow(
                BackfillSpeciesPredictionsWorkflow.run,
                TARGET,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )

    assert attached == 3
    assert EVENTS == [("backfill", TARGET)]
