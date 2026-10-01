"""Workflow contract tests for the calibration parents: stage 13 (slate), the
checkerboard, and the lattice study.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_perform_laser_calibration_parent_workflow.py,
test_checkerboard_calibration_parent.py and
test_verify_checkerboard_lattice_parent.py. Names and reasons are v1's.

v2 changes, each pinned here:

* **the orchestrator records the result.** v1's data-worker PUT the
  extrinsics, or recorded the refusal and raised a non-retryable error, from
  inside the child. The processor has no database now: the child returns a
  `LaserCalibrationResult` and the parent appends it with its provenance.
  A refusal is recorded and then raised non-retryably, under v1's error type,
  so the run still fails loud -- and a failure to record never masks it (v1's
  `test_a_failure_to_record_does_not_mask_the_refusal`);
* stage 13 resolves its inputs before waking anything (v1's child read them
  itself), so a dive with no slate labels wakes no pod (v1: a no-op child);
* the lattice study names its tenant and its dives by number;
* children run on the processor's queues, with raw-reading ids from
  `raw_scratch_reader_id`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import (
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    CheckerboardCalibrationImage,
    CheckerboardLatticeRender,
    CheckerboardTarget,
    LaserCalibrationResult,
    LatticeImage,
    PerformCheckerboardCalibrationInput,
    SlateCalibrationInput,
    VerifyCheckerboardLatticeInput,
)
from fishsense_services_orchestrator.calibration.contracts import (
    CalibrationProvenance,
    CheckerboardCalibrationPlan,
    LatticeDive,
    LatticeImport,
    LatticePlan,
    LatticeProject,
    RecordCalibration,
    SlateCalibrationPlan,
    VerifyCheckerboardLatticeParentInput,
)
from fishsense_services_orchestrator.calibration.workflows import (
    PerformCheckerboardCalibrationParentWorkflow,
    PerformLaserCalibrationParentWorkflow,
    VerifyCheckerboardLatticeParentWorkflow,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)

_K = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]
TENANT = uuid.UUID(int=1)
DIVE = uuid.UUID(int=440)
TARGET = StagingTarget(tenant_id=TENANT, dive_id=DIVE)
CAMERA = uuid.UUID(int=9)
BOARD = CheckerboardTarget(rows=10, cols=14, pitch_x_m=0.042, pitch_y_m=0.042)

#: What ran, in order; what was recorded. Module level: the workflow sandbox
#: re-imports this module, so only activities see the real lists.
_CALLS: list = []
_RECORDED: list = []
_IMPORT_CHUNKS: list = []
_STATE = {"child_fails": False, "record_fails": False, "resolve_fails": set()}


@pytest.fixture(autouse=True)
def _reset():
    for recorded in (_CALLS, _RECORDED, _IMPORT_CHUNKS):
        recorded.clear()
    _STATE.update(child_fails=False, record_fails=False, resolve_fails=set())


def _ref(key: str) -> ObjectRef:
    return ObjectRef(bucket="b", key=f"tenants/{TENANT}/{key}")


ACCEPTED = LaserCalibrationResult(
    outcome="accepted",
    laser_position=[0.0624, 0.0832, 0.0],
    laser_axis=[0.0, 0.0, 1.0],
    observation_count=6,
    observations_trimmed=0,
    gate_verdicts={"observation_geometry": "passed"},
    core_version="4.1.0",
)
REFUSED = LaserCalibrationResult(
    outcome="refused",
    refusal_type="CalibrationImplausibleError",
    refusal_reason="fitted laser baseline 2.35 cm is outside the plausible range",
    observation_count=6,
    observations_trimmed=0,
    gate_verdicts={"baseline_plausible": "refused"},
    core_version="4.1.0",
)
_RESULT = {"value": ACCEPTED}


@activity.defn(name="_record")
async def _record(event: str) -> None:
    _CALLS.append(event)
    if event == "child" and _STATE["child_fails"]:
        raise ValueError("the child's own failure")


@activity.defn(name="record_laser_calibration")
async def _record_calibration(payload: RecordCalibration) -> str:
    _CALLS.append("record")
    if _STATE["record_fails"]:
        raise RuntimeError("the database is down")
    _RECORDED.append(payload)
    return str(uuid.uuid4())


@activity.defn(name="ensure_light_processor_running")
async def _wake_light() -> None:
    _CALLS.append("wake")


@activity.defn(name="ensure_per_image_processor_running")
async def _wake_per_image() -> None:
    _CALLS.append("wake")


@activity.defn(name="stage_raw_bytes_for_dive")
async def _stage(target: StagingTarget) -> StageRawBytesResult:
    _CALLS.append("stage")
    return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)


@activity.defn(name="cleanup_raw_bytes_for_dive")
async def _cleanup(target: StagingTarget) -> CleanupRawBytesResult:
    _CALLS.append("cleanup")
    return CleanupRawBytesResult(deleted=1)


# =============================== stage 13 ===============================


def _slate_plan() -> SlateCalibrationPlan:
    return SlateCalibrationPlan(
        payload=SlateCalibrationInput(
            dive_id=DIVE,
            camera_matrix=_K,
            template_points=[(0.0, 0.0)],
            dpi=300,
            observations=[],
            dive_dots=[],
        ),
        provenance=CalibrationProvenance(
            producer="slate",
            camera_calibration_id=CAMERA,
            slate_template_id=uuid.UUID(int=7),
        ),
    )


@workflow.defn(name="PerformLaserCalibrationWorkflow")
class _StubSlateChild:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: SlateCalibrationInput) -> LaserCalibrationResult:
        await workflow.execute_activity(
            "_record",
            f"child:{workflow.info().workflow_id}",
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return await workflow.execute_activity(
            "_result",
            schedule_to_close_timeout=timedelta(seconds=5),
            result_type=LaserCalibrationResult,
        )


@activity.defn(name="_result")
async def _result() -> LaserCalibrationResult:
    return _RESULT["value"]


def _stage13_stubs(selected, plan):
    @activity.defn(name="select_next_dive_for_laser_calibration")
    async def select() -> StagingTarget | None:
        _CALLS.append("select")
        return selected

    @activity.defn(name="resolve_slate_calibration_inputs")
    async def resolve(target: StagingTarget) -> SlateCalibrationPlan | None:
        _CALLS.append("resolve")
        return plan

    return [select, resolve, _wake_light, _record_calibration]


async def _run_stage13(selected, plan, result=ACCEPTED, *, before=None):
    _RESULT["value"] = result
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue="test-stage13-parent",
                workflows=[PerformLaserCalibrationParentWorkflow],
                activities=_stage13_stubs(selected, plan),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                workflows=[_StubSlateChild],
                activities=[_record, _result],
            ),
        ):
            if before:
                await before(env.client)
            return await env.client.execute_workflow(
                PerformLaserCalibrationParentWorkflow.run,
                id=f"test-stage13-parent-{uuid.uuid4()}",
                task_queue="test-stage13-parent",
            )


async def test_dispatches_child_with_deterministic_id_and_dive_payload():
    result = await _run_stage13(TARGET, _slate_plan())

    assert result == TARGET
    assert f"child:perform-laser-calibration-{DIVE}" in _CALLS
    (recorded,) = _RECORDED
    assert (recorded.tenant_id, recorded.dive_id) == (TENANT, DIVE)
    assert recorded.result == ACCEPTED
    assert recorded.provenance == _slate_plan().provenance


async def test_returns_none_when_selector_finds_no_dive():
    result = await _run_stage13(None, None)

    assert result is None
    assert _CALLS == ["select"]


async def test_a_dive_with_nothing_to_calibrate_wakes_nothing():
    """v1's child returned None for a dive with no slate or no slate labels;
    v2 resolves first, so it dispatches -- and wakes -- nothing."""
    result = await _run_stage13(TARGET, None)

    assert result == TARGET
    assert _CALLS == ["select", "resolve"]


async def test_wakes_the_light_processor_before_dispatching():
    await _run_stage13(TARGET, _slate_plan())

    assert [c.split(":")[0] for c in _CALLS] == [
        "select",
        "resolve",
        "wake",
        "child",
        "record",
    ]


async def test_a_refusal_is_recorded_and_then_fails_the_run():
    """Recorded, so the dive leaves the cohort until its inputs change; then
    raised non-retryably under v1's type, so the failure is loud and names
    the gate."""
    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run_stage13(TARGET, _slate_plan(), REFUSED)

    (recorded,) = _RECORDED
    assert recorded.result == REFUSED
    cause = excinfo.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "CalibrationImplausibleError"
    assert cause.non_retryable
    assert str(DIVE) in str(cause)


async def test_a_failure_to_record_does_not_mask_the_refusal():
    """v1's reason, stated as a test: losing the real refusal -- and
    non_retryable with it -- would be strictly worse than failing to record."""
    _STATE["record_fails"] = True

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run_stage13(TARGET, _slate_plan(), REFUSED)

    assert excinfo.value.cause.type == "CalibrationImplausibleError"


async def test_an_accepted_fit_that_cannot_be_recorded_fails_the_run():
    """The complement: success must never be claimed for a fit not stored."""
    _STATE["record_fails"] = True

    with pytest.raises(WorkflowFailureError):
        await _run_stage13(TARGET, _slate_plan(), ACCEPTED)


async def test_a_child_already_running_records_nothing():
    """Only reachable while a prior child with this id is still running; the
    run that owns it records its result."""

    async def a_child_is_running(client):
        await client.start_workflow(
            "PerformLaserCalibrationWorkflow",
            _slate_plan().payload,
            id=f"perform-laser-calibration-{DIVE}",
            task_queue="no-worker-serves-this-queue",
        )

    result = await _run_stage13(TARGET, _slate_plan(), before=a_child_is_running)

    assert result == TARGET
    assert "record" not in _CALLS


# ============================== checkerboard ==============================


def _board_plan(frames: int) -> CheckerboardCalibrationPlan:
    return CheckerboardCalibrationPlan(
        payload=PerformCheckerboardCalibrationInput(
            dive_id=DIVE,
            camera_matrix=_K,
            distortion_coefficients=[0.0] * 5,
            target=BOARD,
            images=[
                CheckerboardCalibrationImage(
                    capture_id=uuid.UUID(int=100 + n),
                    raw=_ref(f"raw/{n:032d}.ORF"),
                    laser_x=600.0,
                    laser_y=500.0,
                )
                for n in range(frames)
            ],
            dive_dots=[],
        ),
        provenance=CalibrationProvenance(
            producer="checkerboard",
            camera_calibration_id=CAMERA,
            calibration_target_id=uuid.UUID(int=4),
        ),
    )


@workflow.defn(name="PerformCheckerboardCalibrationWorkflow")
class _StubBoardChild:
    # pylint: disable=too-few-public-methods
    """Records that it ran, through an activity (the sandbox re-imports this
    module; activities see the real lists), which is also where it fails."""

    @workflow.run
    async def run(
        self, payload: PerformCheckerboardCalibrationInput
    ) -> LaserCalibrationResult:
        await workflow.execute_activity(
            "_record",
            "child",
            schedule_to_close_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return await workflow.execute_activity(
            "_result",
            schedule_to_close_timeout=timedelta(seconds=5),
            result_type=LaserCalibrationResult,
        )


def _board_stubs(selected, frames):
    @activity.defn(name="select_next_dive_for_checkerboard_calibration")
    async def select() -> StagingTarget | None:
        _CALLS.append("select")
        return selected

    @activity.defn(name="resolve_checkerboard_calibration_inputs")
    async def resolve(target: StagingTarget) -> CheckerboardCalibrationPlan:
        _CALLS.append("resolve")
        return _board_plan(frames)

    return [select, resolve, _wake_per_image, _stage, _cleanup, _record_calibration]


async def _run_board(
    queue, *, selected=TARGET, frames=2, result=ACCEPTED, child_already_running=False
):
    _RESULT["value"] = result
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=queue,
                workflows=[PerformCheckerboardCalibrationParentWorkflow],
                activities=_board_stubs(selected, frames),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_TASK_QUEUE,
                workflows=[_StubBoardChild],
                activities=[_record, _result],
            ),
        ):
            if child_already_running:
                # Occupy the deterministic child id on a queue nobody serves.
                await env.client.start_workflow(
                    _StubBoardChild.run,
                    _board_plan(frames).payload,
                    id=f"perform-checkerboard-calibration-{DIVE}",
                    task_queue="no-worker-serves-this-queue",
                )
            return await env.client.execute_workflow(
                PerformCheckerboardCalibrationParentWorkflow.run,
                id=f"wf-{queue}-{uuid.uuid4()}",
                task_queue=queue,
            )


async def test_parent_stages_dispatches_and_cleans_up():
    assert await _run_board("test-checkerboard-happy") == TARGET
    assert _CALLS == [
        "select",
        "resolve",
        "wake",
        "stage",
        "wake",
        "child",
        "cleanup",
        "record",
    ]


async def test_parent_wakes_the_worker_again_after_staging():
    """Staging outlasts the idle sweeper, so one early wake is not enough: the
    processor queue has nothing running while the orchestrator stages (~30
    minutes for 133 frames), and the sweeper would tear the processor down
    and leave the child hanging on an unserved queue."""
    await _run_board("test-checkerboard-double-wake")

    assert _CALLS.count("wake") == 2
    assert _CALLS.index("child") == _CALLS.index("wake", _CALLS.index("stage")) + 1


async def test_parent_does_nothing_when_the_cohort_is_empty():
    assert await _run_board("test-checkerboard-empty", selected=None) is None
    assert _CALLS == ["select"]


async def test_parent_does_not_stage_when_the_resolver_finds_no_frames():
    """Staging over a ~1 MB/s NAS link to dispatch nothing is the expensive
    way to discover a selector/resolver disagreement."""
    assert await _run_board("test-checkerboard-no-frames", frames=0) == TARGET
    assert _CALLS == ["select", "resolve"]


async def test_parent_leaves_the_scratch_alone_when_a_child_already_owns_it():
    """Another run's child is still reading those `.ORF`s (prod dive 442)."""
    await _run_board("test-checkerboard-already-running", child_already_running=True)

    assert "cleanup" not in _CALLS
    assert "record" not in _CALLS


async def test_parent_cleans_up_even_when_the_child_fails():
    """A child failure must not leave a dive's worth of scratch behind."""
    _STATE["child_fails"] = True

    with pytest.raises(WorkflowFailureError):
        await _run_board("test-checkerboard-child-fails")

    assert "cleanup" in _CALLS
    assert _CALLS.index("cleanup") > _CALLS.index("child")
    assert "record" not in _CALLS


async def test_a_board_refusal_is_recorded_after_cleanup_and_raised():
    """ "No board in these frames" is this stage's expected refusal: the
    scratch is dropped, the refusal recorded, and the run fails loud."""
    refused = REFUSED.model_copy(
        update={
            "refusal_type": "InsufficientCheckerboardPoints",
            "refusal_reason": "insufficient checkerboard laser points (0 < 2)",
        }
    )

    with pytest.raises(WorkflowFailureError) as excinfo:
        await _run_board("test-checkerboard-refused", result=refused)

    assert _CALLS[-2:] == ["cleanup", "record"]
    assert excinfo.value.cause.type == "InsufficientCheckerboardPoints"


# ============================ the lattice study ============================


def _lattice_plan(number: int, frames: int, sample_limit) -> LatticePlan:
    dive = uuid.UUID(int=number)
    return LatticePlan(
        target=StagingTarget(tenant_id=TENANT, dive_id=dive),
        payload=VerifyCheckerboardLatticeInput(
            dive_id=dive,
            camera_matrix=_K,
            distortion_coefficients=[0.0] * 5,
            target=BOARD,
            images=[
                LatticeImage(
                    capture_id=uuid.UUID(int=number * 1000 + n),
                    raw=_ref(f"raw/{number:04d}{n:028d}.ORF"),
                    render=_ref(f"checkerboard_lattice_jpeg/{number:04d}{n:028d}.JPG"),
                    laser_x=600.0,
                    laser_y=500.0,
                )
                for n in range(frames)
            ],
            sample_limit=sample_limit,
        ),
    )


@workflow.defn(name="VerifyCheckerboardLatticeWorkflow")
class _StubLatticeChild:
    # pylint: disable=too-few-public-methods
    """Returns one render per image it was dispatched with."""

    @workflow.run
    async def run(
        self, payload: VerifyCheckerboardLatticeInput
    ) -> List[CheckerboardLatticeRender]:
        images = payload.images
        if payload.sample_limit is not None:
            images = images[: payload.sample_limit]
        return [
            CheckerboardLatticeRender(
                capture_id=image.capture_id,
                image=image.render,
                detected_rows=10,
                detected_cols=14,
                median_spacing_px=32.0,
                corners=[[1.0, 2.0]] * 4,
                width=4000,
                height=3000,
            )
            for image in images
        ]


def _lattice_stubs(frames: int):
    @activity.defn(name="resolve_lattice_tenant")
    async def tenant(slug: str) -> uuid.UUID:
        assert slug == "lab"
        return TENANT

    @activity.defn(name="resolve_lattice_inputs")
    async def resolve(dive: LatticeDive) -> LatticePlan:
        if dive.number in _STATE["resolve_fails"]:
            raise ApplicationError(
                f"dive {dive.number} has no calibration target", non_retryable=True
            )
        return _lattice_plan(dive.number, frames, dive.sample_limit)

    @activity.defn(name="create_checkerboard_lattice_label_studio_project")
    async def create(project: LatticeProject) -> int:
        assert (project.tenant_id, project.tenant_slug) == (TENANT, "lab")
        return 4242

    @activity.defn(name="populate_checkerboard_lattice_label_studio_project")
    async def populate(payload: LatticeImport) -> int:
        _IMPORT_CHUNKS.append([r.capture_id.int for r in payload.renders])
        return len(payload.renders)

    return [tenant, resolve, _wake_per_image, _stage, _cleanup, create, populate]


async def _run_lattice(numbers, *, frames=3, sample_limit=None):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue="lattice-parent-test",
                workflows=[VerifyCheckerboardLatticeParentWorkflow],
                activities=_lattice_stubs(frames),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_TASK_QUEUE,
                workflows=[_StubLatticeChild],
            ),
        ):
            return await env.client.execute_workflow(
                VerifyCheckerboardLatticeParentWorkflow.run,
                VerifyCheckerboardLatticeParentInput(
                    tenant="lab", dives=numbers, sample_limit=sample_limit
                ),
                id=f"wf-lattice-{uuid.uuid4()}",
                task_queue="lattice-parent-test",
                execution_timeout=timedelta(minutes=5),
            )


async def test_imports_every_render_across_every_dive():
    imported = await _run_lattice([1, 2, 3], frames=4)

    assert imported == 12
    assert sum(len(chunk) for chunk in _IMPORT_CHUNKS) == 12


async def test_the_import_is_chunked_rather_than_one_payload():
    """250 renders must not travel as a single activity argument (Temporal's
    2 MB payload limit)."""
    await _run_lattice([1, 2, 3, 4, 5], frames=50)

    assert len(_IMPORT_CHUNKS) > 1
    assert all(len(chunk) <= 100 for chunk in _IMPORT_CHUNKS)


async def test_chunks_are_shuffled_across_dives_not_grouped_by_dive():
    """Chunking before the shuffle would restore per-dive ordering, and Label
    Studio serves tasks in import order -- the blinding the shuffle is for."""
    await _run_lattice([1, 2, 3, 4, 5], frames=50)

    # v1 asserted "more than one dive", which an unshuffled import also
    # passes: 100 renders in dive order span two dives of 50. Shuffled, a
    # chunk of 100 from 5 x 50 misses a dive with negligible probability.
    first_chunk_dives = {capture // 1000 for capture in _IMPORT_CHUNKS[0]}
    assert first_chunk_dives == {1, 2, 3, 4, 5}


async def test_one_failing_dive_does_not_discard_the_others():
    """The expensive failure: staging is done by the time this could bite."""
    _STATE["resolve_fails"] = {2}

    imported = await _run_lattice([1, 2, 3], frames=4)

    assert imported == 8
    assert {c // 1000 for chunk in _IMPORT_CHUNKS for c in chunk} == {1, 3}


async def test_every_dive_failing_imports_nothing_and_does_not_raise():
    """A study of nothing is a reportable outcome, not a crash."""
    _STATE["resolve_fails"] = {1, 2}

    imported = await _run_lattice([1, 2], frames=4)

    assert imported == 0
    assert not _IMPORT_CHUNKS


async def test_sample_limit_reaches_the_child():
    imported = await _run_lattice([1, 2], frames=10, sample_limit=3)

    assert imported == 6


async def test_every_dive_is_staged_and_cleaned_up():
    await _run_lattice([1, 2], frames=2)

    assert _CALLS.count("stage") == 2
    assert _CALLS.count("cleanup") == 2
    assert _CALLS.count("wake") == 4, "twice per dive, around the staging"
