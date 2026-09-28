"""Workflow contract test for PreprocessSpeciesImagesParentWorkflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_preprocess_species_images_parent_workflow.py (6). Test names, bodies
and reasons are v1's; v2 adaptations: the target is (tenant, dive); the child
goes to the processor's per-image queue under the id `raw_scratch_reader_id`
gives it; the wake stands the per-image processor up
(`ensure_per_image_processor_running`, v1: `ensure_data_worker_running_activity`);
and the flag clear is scoped by capture (v1: by checksum).

Pinned too, from v1's `_dispatch`: the steps run wake -> stage -> child ->
cleanup -> clear, and a firing that finds the child still running (a manual run
overlapping the schedule) leaves the raw scratch and the flags to the run that
owns it (prod dive 442: 984 raw objects deleted under a running child).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import List

from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species import (
    PreprocessSpeciesImagesInput,
    SpeciesClusterMember,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.species.contracts import (
    ClearReprocessFlagsInput,
    SpeciesTarget,
)
from fishsense_services_orchestrator.species.workflows import (
    PreprocessSpeciesImagesParentWorkflow,
)

_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [-0.1, 0.05, 0.0, 0.0, 0.0]
TENANT = uuid.uuid4()
QUEUE = "test-stage2-parent"


def _capture(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_OID, name)


def _member(name: str, index: int = 1, size: int = 1) -> SpeciesClusterMember:
    checksum = (name * 32)[:32]
    return SpeciesClusterMember(
        capture_id=_capture(name),
        raw=ObjectRef(bucket="s", key=f"tenants/{TENANT}/raw/{checksum}.ORF"),
        jpeg=ObjectRef(
            bucket="l", key=f"tenants/{TENANT}/preprocess_groups_jpeg/{checksum}.JPG"
        ),
        cluster_index=index,
        cluster_size=size,
    )


def _inputs(dive, clusters) -> PreprocessSpeciesImagesInput:
    return PreprocessSpeciesImagesInput(
        dive_id=dive,
        camera_matrix=_K,
        distortion_coefficients=_D,
        cluster_members=[
            [_member(n, i + 1, len(cluster)) for i, n in enumerate(cluster)]
            for cluster in clusters
        ],
    )


class Recorder:
    def __init__(self):
        self.events: List[str] = []
        self.children: List[tuple] = []
        self.clear_scopes: List = []
        self.resolved: List[SpeciesTarget] = []


@workflow.defn(name="PreprocessSpeciesImagesWorkflow")
class _StubChildWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessSpeciesImagesInput) -> None:
        flat = [m.capture_id for group in payload.cluster_members for m in group]
        await workflow.execute_activity(
            "_record_child_dispatch",
            args=(workflow.info().workflow_id, payload.dive_id, flat),
            schedule_to_close_timeout=timedelta(seconds=5),
        )


def _record_child(recorder: Recorder):
    @activity.defn(name="_record_child_dispatch")
    async def record(workflow_id: str, dive_id: uuid.UUID, captures: List[uuid.UUID]):
        recorder.events.append("child")
        recorder.children.append((workflow_id, dive_id, captures))

    return record


def _stubs(recorder: Recorder, target, inputs):
    @activity.defn(name="select_next_dive_for_species_preprocessing")
    async def stub_select() -> SpeciesTarget | None:
        recorder.events.append("select")
        return target

    @activity.defn(name="resolve_species_preprocess_inputs")
    async def stub_resolve(t: SpeciesTarget) -> PreprocessSpeciesImagesInput:
        recorder.resolved.append(t)
        return inputs

    @activity.defn(name="ensure_per_image_processor_running")
    async def stub_wake() -> int:
        recorder.events.append("wake")
        return 1

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stub_stage(t: StagingTarget) -> StageRawBytesResult:
        recorder.events.append("stage")
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def stub_cleanup(t: StagingTarget) -> CleanupRawBytesResult:
        recorder.events.append("cleanup")
        return CleanupRawBytesResult(deleted=1)

    @activity.defn(name="clear_species_reprocess_flags")
    async def stub_clear(payload: ClearReprocessFlagsInput) -> int:
        """The parent lowers the redraw flag after its child completes;
        without it the dive stays in the cohort forever."""
        recorder.events.append("clear")
        recorder.clear_scopes.append(payload.capture_ids)
        return 0

    return [stub_select, stub_resolve, stub_wake, stub_stage, stub_cleanup, stub_clear]


async def _run(target, inputs, *, runs=1, before=None) -> tuple[list, Recorder]:
    recorder = Recorder()
    results = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PreprocessSpeciesImagesParentWorkflow],
                activities=_stubs(recorder, target, inputs),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_TASK_QUEUE,
                workflows=[_StubChildWorkflow],
                activities=[_record_child(recorder)],
            ),
        ):
            if before is not None:
                await before(env.client)
            for _ in range(runs):
                # A child sent to a queue nothing serves isn't an error, it
                # hangs (v1's warning): bound the wait so that fails here.
                results.append(
                    await asyncio.wait_for(
                        env.client.execute_workflow(
                            PreprocessSpeciesImagesParentWorkflow.run,
                            id=f"{QUEUE}-{uuid.uuid4()}",
                            task_queue=QUEUE,
                        ),
                        timeout=60,
                    )
                )
    return results, recorder


async def test_dispatches_child_with_deterministic_id_and_clusters():
    dive = uuid.uuid4()
    target = SpeciesTarget(TENANT, dive)

    (result,), recorder = await _run(target, _inputs(dive, [["a", "b"], ["c"]]))

    assert result == target
    assert recorder.events.count("select") == 1
    assert recorder.resolved == [target]
    ((child_id, child_dive, flat),) = recorder.children
    assert child_id == f"preprocess-species-{dive}"
    assert child_dive == dive
    assert flat == [_capture("a"), _capture("b"), _capture("c")]


async def test_returns_none_when_selector_finds_no_dive():
    (result,), recorder = await _run(None, None)

    assert result is None
    assert recorder.resolved == []
    assert recorder.children == []
    assert recorder.events == ["select"], "nothing woken, staged or cleared"


async def test_skips_child_dispatch_when_no_clusters():
    dive = uuid.uuid4()

    (result,), recorder = await _run(SpeciesTarget(TENANT, dive), _inputs(dive, []))

    assert result == SpeciesTarget(TENANT, dive)
    assert recorder.children == []
    # A quiet dive neither wakes a pod nor stages the NAS.
    assert "wake" not in recorder.events and "stage" not in recorder.events


async def test_child_redispatches_after_a_prior_successful_run():
    """The preprocess child must re-run on a dive it already processed.

    A dive's frame set grows after its first successful child run — a laser
    validated after one-shot stage-1 clustering, or an orphan later given a
    cluster. Under ALLOW_DUPLICATE_FAILED_ONLY the deterministic child id was
    permanently spent once it completed, so those frames' JPEGs were never
    produced and populate deferred them forever (prod dives 59/439).
    """
    dive = uuid.uuid4()

    _, recorder = await _run(
        SpeciesTarget(TENANT, dive), _inputs(dive, [["orphan"]]), runs=2
    )

    # Same deterministic child id both times; both must have run.
    assert len(recorder.children) == 2, "child must re-dispatch on the second run"
    assert {c[0] for c in recorder.children} == {f"preprocess-species-{dive}"}


async def test_lowers_the_reprocess_flag_even_when_no_work_resolves():
    """A flag that reaches no image must still be lowered, whole-dive: it is
    the one term of the cohort that does not go false on its own, and left up
    it re-selects the dive every hour, re-staging its raw frames."""
    dive = uuid.uuid4()

    (result,), recorder = await _run(SpeciesTarget(TENANT, dive), _inputs(dive, []))

    assert result == SpeciesTarget(TENANT, dive)
    assert recorder.clear_scopes == [None], (
        "the no-work path clears the WHOLE dive on purpose -- the flag reached "
        "no image, so nothing else will ever lower it"
    )


async def test_the_successful_path_clears_only_what_it_redrew():
    """A flag raised while the child is running must survive the clear: the
    child can run two hours, and an unscoped clear would lower a request this
    run never saw."""
    dive = uuid.uuid4()

    _, recorder = await _run(
        SpeciesTarget(TENANT, dive), _inputs(dive, [["aa", "bb"], ["cc"]])
    )

    assert recorder.clear_scopes == [
        [_capture("aa"), _capture("bb"), _capture("cc")]
    ], "every redrawn frame, and nothing else"


async def test_steps_run_wake_stage_child_cleanup_clear():
    dive = uuid.uuid4()

    _, recorder = await _run(SpeciesTarget(TENANT, dive), _inputs(dive, [["a"]]))

    assert recorder.events == ["select", "wake", "stage", "child", "cleanup", "clear"]


@workflow.defn(name="PreprocessSpeciesImagesWorkflow")
class _StillRunningChild:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessSpeciesImagesInput) -> None:
        await workflow.wait_condition(lambda: False)


async def test_a_child_still_running_keeps_its_scratch_and_flags():
    """If another firing's child is still running, this one dispatched
    nothing and must not clean up or clear after the run that owns it."""
    dive = uuid.uuid4()
    started: List[WorkflowHandle] = []

    async def start_owner(client):
        started.append(
            await client.start_workflow(
                _StillRunningChild.run,
                _inputs(dive, [["a"]]),
                id=f"preprocess-species-{dive}",
                task_queue="owner-queue",
            )
        )

    recorder = Recorder()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PreprocessSpeciesImagesParentWorkflow],
                activities=_stubs(
                    recorder, SpeciesTarget(TENANT, dive), _inputs(dive, [["a"]])
                ),
            ),
            Worker(
                env.client, task_queue="owner-queue", workflows=[_StillRunningChild]
            ),
        ):
            await start_owner(env.client)
            await asyncio.sleep(0)
            result = await env.client.execute_workflow(
                PreprocessSpeciesImagesParentWorkflow.run,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )

    assert result == SpeciesTarget(TENANT, dive)
    assert recorder.events == [
        "select",
        "wake",
        "stage",
    ], "refused the duplicate child, then neither cleaned up nor cleared"
    assert recorder.clear_scopes == []
