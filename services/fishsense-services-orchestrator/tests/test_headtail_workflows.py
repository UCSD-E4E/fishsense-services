"""Workflow contract tests for the head/tail parents and their children.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/:
test_preprocess_headtail_images_parent_workflow.py,
test_predict_headtail_images_parent_workflow.py,
test_populate_label_studio_project_workflows.py and
test_populate_workflow_retry_policy.py (the head/tail rows),
test_sync_label_studio_headtail_labels_workflow.py, and the dispatch rules of
workflows/_dispatch.py. Names, bodies and reasons are v1's; the v2 adaptations:
the target is (tenant, dive); the children go to the processor's queues; the
wakes stand the processor up (`nrp.workflow`); staging and cleanup are the
object store's steps; activities have v2's names.

v2 changes, each pinned:

* **a predict child that is already running means do nothing more** -- v1's
  predict parent iterated `CHILD_ALREADY_RUNNING` as if it were the results;
* one project's sync failure doesn't cancel the others (the laser port's rule);
* the sync has no user-sync step (v2 records Label Studio ids directly).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_contracts.headtail import (
    HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
    HeadtailPredictionResult,
    PredictHeadtailImage,
    PredictHeadtailImagesInput,
    PreprocessHeadtailImage,
    PreprocessHeadtailImagesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.headtail import workflow as sut
from fishsense_services_orchestrator.headtail.activities import (
    ClearHeadtailReprocessFlags,
    HeadtailTarget,
)
from fishsense_services_orchestrator.headtail.workflow import (
    BackfillHeadtailPredictionsWorkflow,
    CreateHeadTailLabelStudioProjectWorkflow,
    PopulateHeadTailLabelStudioProjectParentWorkflow,
    PopulateHeadTailLabelStudioProjectWorkflow,
    PredictHeadtailImagesParentWorkflow,
    PreprocessHeadtailImagesParentWorkflow,
    SyncLabelStudioHeadTailLabelsWorkflow,
)
from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = HeadtailTarget(TENANT, DIVE)
K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
QUEUE = "test-headtail-parent"

#: What each run did, in order. Module-level so the stub children, which run
#: in Temporal's sandbox, report through an activity rather than directly.
EVENTS: list[tuple] = []
#: What the stub predict child returns, read through an activity for the same
#: reason (a workflow body sees a fresh copy of this module).
CHILD_RESULTS: list[HeadtailPredictionResult] = []


@pytest.fixture(autouse=True)
def _reset():
    EVENTS.clear()
    CHILD_RESULTS.clear()


def _ref(checksum):
    return ObjectRef(
        bucket="labels", key=f"tenants/{TENANT}/preprocess_headtail_jpeg/{checksum}.JPG"
    )


def _preprocess_inputs(checksums):
    return PreprocessHeadtailImagesInput(
        tenant_id=TENANT,
        dive_id=DIVE,
        images=[
            PreprocessHeadtailImage(
                capture_id=uuid.uuid4(),
                checksum=c,
                raw=ObjectRef(bucket="scratch", key=f"tenants/{TENANT}/raw/{c}.ORF"),
                jpeg=_ref(c),
            )
            for c in checksums
        ],
        camera_matrix=K,
        distortion_coefficients=D,
    )


def _predict_inputs(n=1):
    return PredictHeadtailImagesInput(
        tenant_id=TENANT,
        dive_id=DIVE,
        images=[
            PredictHeadtailImage(
                capture_id=uuid.uuid4(),
                jpeg=_ref(f"{i:032x}"),
                laser_points=[[10.0, 10.0]],
                laser_label_ids=[uuid.uuid4()],
            )
            for i in range(n)
        ],
    )


@activity.defn(name="_record")
async def _record(event: List[str]) -> None:
    EVENTS.append(tuple(event))


@activity.defn(name="_child_results")
async def _child_results() -> List[HeadtailPredictionResult]:
    return list(CHILD_RESULTS)


@workflow.defn(name="PreprocessHeadtailImagesWorkflow")
class _StubPreprocessChild:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessHeadtailImagesInput) -> None:
        await workflow.execute_activity(
            "_record",
            [
                "child",
                workflow.info().workflow_id,
                *[i.checksum for i in payload.images],
            ],
            schedule_to_close_timeout=timedelta(seconds=5),
        )


@workflow.defn(name="PredictHeadtailImagesWorkflow")
class _StubPredictChild:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(
        self, payload: PredictHeadtailImagesInput
    ) -> List[HeadtailPredictionResult]:
        await workflow.execute_activity(
            "_record",
            ["child", workflow.info().workflow_id],
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return await workflow.execute_activity(
            "_child_results",
            schedule_to_close_timeout=timedelta(seconds=5),
            result_type=List[HeadtailPredictionResult],
        )


@workflow.defn(name="PopulateHeadTailLabelStudioProjectWorkflow")
class _StubPopulateChild:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: HeadtailTarget) -> int:
        await workflow.execute_activity(
            "_record",
            ["populate", workflow.info().workflow_id],
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        if str(target.dive_id).endswith("0bad"):
            # An ApplicationError fails the workflow; a plain exception would
            # only fail its task, which Temporal retries forever.
            raise ApplicationError("this dive's Label Studio project is broken")
        return 1


def _preprocess_stubs(selected, inputs):
    @activity.defn(name="select_next_dive_for_headtail_preprocessing")
    async def select() -> HeadtailTarget | None:
        return selected

    @activity.defn(name="resolve_headtail_preprocess_inputs")
    async def resolve(target: HeadtailTarget) -> PreprocessHeadtailImagesInput:
        return inputs

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(target: StagingTarget) -> StageRawBytesResult:
        EVENTS.append(("stage", target.tenant_id, target.dive_id))
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup(target: StagingTarget) -> CleanupRawBytesResult:
        EVENTS.append(("cleanup", target.dive_id))
        return CleanupRawBytesResult(deleted=1)

    @activity.defn(name="clear_headtail_reprocess_flags")
    async def clear(request: ClearHeadtailReprocessFlags) -> int:
        EVENTS.append(("clear", request.dive_id, request.checksums))
        return 0

    return [select, resolve, wake, stage, cleanup, clear, _record]


async def _run_preprocess(selected, inputs, before=None):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PreprocessHeadtailImagesParentWorkflow],
                activities=_preprocess_stubs(selected, inputs),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_TASK_QUEUE,
                workflows=[_StubPreprocessChild],
                activities=[_record],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                PreprocessHeadtailImagesParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


def _kinds():
    return [e[0] for e in EVENTS]


# -- stage 5.1 (v1's test_preprocess_headtail_images_parent_workflow.py) -----------


async def test_dispatches_child_with_deterministic_id():
    result = await _run_preprocess(TARGET, _preprocess_inputs(["a", "b"]))

    assert result == TARGET
    (child,) = [e for e in EVENTS if e[0] == "child"]
    assert child == ("child", f"preprocess-headtail-{DIVE}", "a", "b")
    # Populate is NOT chained here: it is its own +34 parent, behind the +32
    # predict parent. Chaining would seed rows before the detector ran and
    # remove every image from the predict cohort -- silently.
    assert "populate" not in _kinds()


async def test_the_steps_run_in_v1s_order_and_the_clear_is_scoped():
    """Wake, stage, render, clean up, then lower exactly the flags of the
    frames this run redrew: one raised mid-run survives for the next firing."""
    await _run_preprocess(TARGET, _preprocess_inputs(["a", "b"]))

    assert _kinds() == ["wake", "stage", "child", "cleanup", "clear"]
    assert EVENTS[1] == ("stage", TENANT, DIVE)
    assert EVENTS[-1] == ("clear", DIVE, ["a", "b"])


async def test_returns_none_when_no_dive():
    assert await _run_preprocess(None, None) is None
    assert not EVENTS


async def test_lowers_the_reprocess_flag_even_when_no_work_resolves():
    """The flag is the one cohort term that does not go false on its own: if
    the no-work return skipped the clear, the dive would re-stage its raw
    frames from the NAS every hour, forever."""
    result = await _run_preprocess(TARGET, _preprocess_inputs([]))

    assert result == TARGET
    assert EVENTS == [("clear", DIVE, None)], "whole dive; no wake, stage or child"


async def test_a_child_already_running_means_no_cleanup_and_no_clear():
    """Prod dive 442, 2026-09-07: a refused duplicate dispatch went on to
    delete 984 raw objects under the running child and clear 515 flags."""

    async def a_child_is_running(client):
        await client.start_workflow(
            "PreprocessHeadtailImagesWorkflow",
            _preprocess_inputs(["x"]),
            id=f"preprocess-headtail-{DIVE}",
            task_queue="nobody-polls-this",
        )

    result = await _run_preprocess(
        TARGET, _preprocess_inputs(["a"]), before=a_child_is_running
    )

    assert result == TARGET
    assert _kinds() == ["wake", "stage"]


# -- predict (v1's test_predict_headtail_images_parent_workflow.py) -------------------


def _predict_stubs(*, mode="gpu", images=1):
    @activity.defn(name="select_next_dive_for_headtail_prediction")
    async def select() -> HeadtailTarget | None:
        return TARGET

    @activity.defn(name="resolve_headtail_predict_inputs")
    async def resolve(target: HeadtailTarget) -> PredictHeadtailImagesInput:
        return _predict_inputs(images)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return mode

    @activity.defn(name="persist_headtail_predictions")
    async def persist(
        target: HeadtailTarget, results: List[HeadtailPredictionResult]
    ) -> int:
        EVENTS.append(("persist", [r.status for r in results]))
        return len(results)

    @activity.defn(name="backfill_headtail_predictions_for_dive")
    async def backfill(target: HeadtailTarget) -> int:
        EVENTS.append(("backfill", target.dive_id))
        return 0

    return [select, resolve, wake, persist, backfill, _record, _child_results]


async def _run_predict(child_results=(), before=None, **stubs):
    CHILD_RESULTS.extend(child_results)
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        # The stub child is on the processor's GPU queue and nowhere else: a
        # regression dispatching elsewhere hangs rather than passes.
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PredictHeadtailImagesParentWorkflow],
                activities=_predict_stubs(**stubs),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                workflows=[_StubPredictChild],
                activities=[_record, _child_results],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                PredictHeadtailImagesParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


def _predicted():
    return HeadtailPredictionResult(
        capture_id=uuid.uuid4(), status="predicted", head_x=1.0, head_y=2.0,
        tail_x=3.0, tail_y=4.0, predictor_version=2,
    )  # fmt: skip


def _skipped():
    return HeadtailPredictionResult(
        capture_id=uuid.uuid4(),
        status=HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
        predictor_version=-1,
    )


async def test_persists_and_backfills_a_normal_run():
    assert await _run_predict([_predicted()]) == TARGET
    assert EVENTS == [
        ("wake",),
        ("child", f"predict-headtail-{DIVE}"),
        ("persist", ["predicted"]),
        ("backfill", DIVE),
    ]


async def test_skips_are_never_persisted():
    """Persisting a skip marker would blank a perfectly good prediction."""
    await _run_predict([_predicted(), _skipped()])
    assert ("persist", ["predicted"]) in EVENTS


async def test_an_all_skip_run_still_backfills():
    """The regression v1's file exists for: an all-fallback dive re-offered on
    CPU capacity has nothing to persist, but its project may still point at no
    version, leaving every attached prediction invisible."""
    await _run_predict([_skipped(), _skipped()])
    assert "persist" not in _kinds()
    assert ("backfill", DIVE) in EVENTS


async def test_no_worker_available_means_no_child():
    """A child on an unserved queue doesn't fail, it hangs until its 6h
    timeout; the dive stays in the cohort for the next firing."""
    assert await _run_predict(mode="unavailable") is None
    assert _kinds() == ["wake"]


async def test_no_images_needs_no_worker():
    assert await _run_predict(images=0) == TARGET
    assert not EVENTS


async def test_the_cpu_fallback_serves_the_same_queue():
    assert await _run_predict([_predicted()], mode="cpu_fallback") == TARGET
    assert ("backfill", DIVE) in EVENTS


async def test_a_predict_child_already_running_means_do_nothing_more():
    """v2 fix: v1 iterated its CHILD_ALREADY_RUNNING sentinel as the results.
    The run that owns the child persists and backfills."""

    async def a_child_is_running(client):
        await client.start_workflow(
            "PredictHeadtailImagesWorkflow",
            _predict_inputs(),
            id=f"predict-headtail-{DIVE}",
            task_queue="nobody-polls-this",
        )

    assert await _run_predict(before=a_child_is_running) == TARGET
    assert _kinds() == ["wake"]


# -- populate (v1's populate parent and workflow tests) -------------------------------


async def test_populate_parent_fans_out_one_child_per_dive_and_survives_a_failure():
    """ALLOW_DUPLICATE, id `populate-headtail-{dive}`; one dive's failure must
    not abort the fan-out (v1's `suppress=True`)."""
    good = HeadtailTarget(TENANT, uuid.uuid4())
    bad = HeadtailTarget(TENANT, uuid.UUID(f"{uuid.uuid4().hex[:28]}0bad"))

    @activity.defn(name="select_dives_needing_headtail_population")
    async def select() -> List[HeadtailTarget]:
        return [bad, good]

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[
                PopulateHeadTailLabelStudioProjectParentWorkflow,
                _StubPopulateChild,
            ],
            activities=[select, _record],
        ):
            result = await env.client.execute_workflow(
                PopulateHeadTailLabelStudioProjectParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )

    assert result == [bad, good]
    assert sorted(e[1] for e in EVENTS) == sorted(
        [f"populate-headtail-{bad.dive_id}", f"populate-headtail-{good.dive_id}"]
    )


async def test_populate_parent_with_nothing_to_do_dispatches_nothing():
    @activity.defn(name="select_dives_needing_headtail_population")
    async def select() -> List[HeadtailTarget]:
        return []

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[PopulateHeadTailLabelStudioProjectParentWorkflow],
            activities=[select],
        ):
            result = await env.client.execute_workflow(
                PopulateHeadTailLabelStudioProjectParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
    assert result == []


async def test_populate_creates_the_project_then_populates_it():
    calls = []

    @activity.defn(name="create_headtail_label_studio_project")
    async def create(target: HeadtailTarget) -> int:
        calls.append(("create", target))
        return 555

    @activity.defn(name="populate_headtail_label_studio_project")
    async def populate(target: HeadtailTarget, project_id: int) -> int:
        calls.append(("populate", target, project_id))
        return 7

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[
                PopulateHeadTailLabelStudioProjectWorkflow,
                CreateHeadTailLabelStudioProjectWorkflow,
            ],
            activities=[create, populate],
        ):
            populated = await env.client.execute_workflow(
                PopulateHeadTailLabelStudioProjectWorkflow.run,
                TARGET,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            created = await env.client.execute_workflow(
                CreateHeadTailLabelStudioProjectWorkflow.run,
                TARGET,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )

    assert (populated, created) == (7, 555)
    assert calls[:2] == [("create", TARGET), ("populate", TARGET, 555)]


@pytest.fixture(name="activity_calls")
def _activity_calls(monkeypatch):
    seen = []

    async def fake_execute_activity(name, *args, **kwargs):
        seen.append({"name": name, "args": args, **kwargs})
        return 7 if name.startswith("create_") else 42

    monkeypatch.setattr(workflow, "execute_activity", fake_execute_activity)
    return seen


async def test_populate_timeouts_and_bounded_retries_are_v1s(activity_calls):
    """Unlimited retries let dive 424 reach attempt 10 and 23 copies of three
    frames; capping attempts alone would shrink the window to ~3s. v1's
    policy: 30s initial, x2, at most 5 min apart, 5 attempts."""
    assert await sut.create_then_populate(TARGET) == 42

    create, populate = activity_calls
    assert create["name"] == "create_headtail_label_studio_project"
    assert create["schedule_to_close_timeout"] == timedelta(minutes=5)
    assert populate["name"] == "populate_headtail_label_studio_project"
    assert create["args"] == (TARGET,)
    assert populate["args"] == (TARGET, 7), "populate takes the created project id"
    assert populate["schedule_to_close_timeout"] == timedelta(minutes=30)
    assert populate["heartbeat_timeout"] == timedelta(minutes=2)
    policy = populate["retry_policy"]
    assert policy.maximum_attempts == 5
    assert policy.initial_interval == timedelta(seconds=30)
    assert policy.backoff_coefficient == 2.0
    assert policy.maximum_interval == timedelta(minutes=5)


# -- the backfill, on demand ------------------------------------------------------------


async def test_backfill_workflow_runs_the_dives_backfill():
    @activity.defn(name="backfill_headtail_predictions_for_dive")
    async def backfill(target: HeadtailTarget) -> int:
        EVENTS.append(("backfill", target))
        return 3

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[BackfillHeadtailPredictionsWorkflow],
            activities=[backfill],
        ):
            attached = await env.client.execute_workflow(
                BackfillHeadtailPredictionsWorkflow.run,
                TARGET,
                id=f"backfill-headtail-predictions-{DIVE}",
                task_queue=QUEUE,
            )

    assert attached == 3
    assert EVENTS == [("backfill", TARGET)]


# -- the sync (v1's test_sync_label_studio_headtail_labels_workflow.py) ----------------


async def _run_sync(fail_projects=()):
    synced = []

    @activity.defn(name="head_tail_label_projects")
    async def projects() -> List[LabelProject]:
        return [LabelProject(TENANT, 11), LabelProject(TENANT, 22)]

    @activity.defn(name="sync_head_tail_labels")
    async def sync(project: LabelProject) -> None:
        if project.ls_project_id in fail_projects:
            raise ApplicationError("Label Studio lost this one", non_retryable=True)
        synced.append(project.ls_project_id)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[SyncLabelStudioHeadTailLabelsWorkflow],
            activities=[projects, sync],
        ):
            handle = await env.client.start_workflow(
                SyncLabelStudioHeadTailLabelsWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            try:
                await handle.result()
            except WorkflowFailureError as exc:
                return synced, exc
    return synced, None


async def test_workflow_invokes_one_sync_per_project():
    synced, failure = await _run_sync()
    assert sorted(synced) == [11, 22]
    assert failure is None


async def test_one_projects_failure_does_not_stop_the_others():
    """v2 (the laser port's rule): v1's TaskGroup cancelled every project on
    the first failure; here each finishes, and the run then fails naming it."""
    synced, failure = await _run_sync(fail_projects={11})
    assert synced == [22]
    assert failure is not None and "[11]" in str(failure.cause)


def test_v1s_fan_out_bounds():
    """At most four project syncs, and four dive populates, at once."""
    assert sut.PROJECT_CONCURRENCY == 4 and sut.POPULATE_CONCURRENCY == 4


def test_children_reuse_their_ids_whatever_the_last_one_did():
    """ALLOW_DUPLICATE, never FAILED_ONLY: a completed child must not burn
    the id, or a dive gaining work later never gets it done (prod dive 60)."""
    assert sut.CHILD_ID_REUSE == WorkflowIDReusePolicy.ALLOW_DUPLICATE
