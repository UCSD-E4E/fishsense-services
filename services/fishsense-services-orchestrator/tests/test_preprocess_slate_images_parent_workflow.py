"""Workflow contract test for PreprocessSlateImagesParentWorkflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_preprocess_slate_images_parent_workflow.py. Names, bodies and
reasons are v1's; v2 adaptations: the target is (tenant, dive), the resolver
returns the processor's payload (refs, not checksums) plus the checksums the
flags are scoped to, the child goes to the processor's per-image queue, the
wake is `ensure_per_image_processor_running`, and the slate PDF is staged by
template.

v2 additions, pinned last: the child's id comes from `raw_scratch_reader_id`
(the cleanup gate must know every raw reader), and a child already running
under another firing leaves the scratch and the flags alone (v1's
`CHILD_ALREADY_RUNNING`, prod dive 442).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    PreprocessSlateImage,
    PreprocessSlateImagesInput,
)
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.slates.contracts import (
    ClearSlateFlagsInput,
    PopulateSlateProject,
    SlatePdfTarget,
    SlatePreprocessPlan,
)
from fishsense_services_orchestrator.slates.workflows import (
    PreprocessSlateImagesParentWorkflow,
)

_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [-0.1, 0.05, 0.0, 0.0, 0.0]
TENANT = uuid.UUID(int=1)
DIVE = uuid.UUID(int=440)
SLATE = uuid.UUID(int=7)
TARGET = StagingTarget(tenant_id=TENANT, dive_id=DIVE)
QUEUE = "test-stage9-parent"

#: What ran, in order; the clear calls' scopes. Module level: the workflow
#: sandbox re-imports this module, so only activities see the real lists.
_EVENTS: list = []
_CLEAR_SCOPES: list = []
_POPULATES: list = []


@pytest.fixture(autouse=True)
def _reset():
    for recorded in (_EVENTS, _CLEAR_SCOPES, _POPULATES):
        recorded.clear()


def _plan(checksums: list[str]) -> SlatePreprocessPlan:
    ref = lambda key: ObjectRef(bucket="b", key=f"tenants/{TENANT}/{key}")  # noqa: E731
    return SlatePreprocessPlan(
        payload=PreprocessSlateImagesInput(
            dive_id=DIVE,
            slate_template_id=SLATE,
            slate_pdf=ref(f"slate_pdf/{SLATE}.pdf"),
            slate_dpi=300,
            reference_points=[(0.0, 0.0), (1.0, 1.0)],
            camera_matrix=_K,
            distortion_coefficients=_D,
            images=[
                PreprocessSlateImage(
                    capture_id=uuid.uuid5(uuid.NAMESPACE_URL, c),
                    raw=ref(f"raw/{c}.ORF"),
                    jpeg=ref(f"preprocess_slate_images_jpeg/{c}.JPG"),
                )
                for c in checksums
            ],
        ),
        checksums=checksums,
    )


@workflow.defn(name="PreprocessSlateImagesWorkflow")
class _StubChildWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: PreprocessSlateImagesInput) -> None:
        await workflow.execute_activity(
            "_record",
            args=("child", workflow.info().workflow_id),
            schedule_to_close_timeout=timedelta(seconds=5),
        )


@workflow.defn(name="PopulateDiveSlateLabelStudioProjectWorkflow")
class _StubPopulateWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: StagingTarget) -> int:
        await workflow.execute_activity(
            "_record",
            args=("populate", workflow.info().workflow_id),
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        return 0


@activity.defn(name="_record")
async def _record(event: str, workflow_id: str) -> None:
    _EVENTS.append(event)
    if event == "populate":
        _POPULATES.append(workflow_id)
    if event == "child":
        _EVENTS.append(f"child:{workflow_id}")


def _stubs(selector_result, plan):
    @activity.defn(name="select_next_dive_for_slate_preprocessing")
    async def select() -> StagingTarget | None:
        return selector_result

    @activity.defn(name="resolve_slate_preprocess_inputs")
    async def resolve(target: StagingTarget) -> SlatePreprocessPlan:
        assert target == TARGET
        return plan

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        _EVENTS.append("wake")

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(target: StagingTarget) -> StageRawBytesResult:
        _EVENTS.append("stage")
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="stage_slate_pdf")
    async def stage_pdf(target: SlatePdfTarget) -> bool:
        assert (target.tenant_id, target.slate_template_id) == (TENANT, SLATE)
        _EVENTS.append("stage_pdf")
        return True

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup(target: StagingTarget) -> CleanupRawBytesResult:
        _EVENTS.append("cleanup")
        return CleanupRawBytesResult(deleted=1)

    @activity.defn(name="clear_slate_reprocess_flags")
    async def clear(payload: ClearSlateFlagsInput) -> int:
        """The parent lowers the redraw flag after its child completes;
        without it the dive stays in the cohort forever."""
        assert (payload.tenant_id, payload.dive_id) == (TENANT, DIVE)
        _EVENTS.append("clear")
        _CLEAR_SCOPES.append(payload.checksums)
        return 0

    return [select, resolve, wake, stage, stage_pdf, cleanup, clear, _record]


async def _run(selector_result, plan, *, firings=1, before=None):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[PreprocessSlateImagesParentWorkflow, _StubPopulateWorkflow],
                activities=_stubs(selector_result, plan),
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_TASK_QUEUE,
                workflows=[_StubChildWorkflow],
                activities=[_record],
            ),
        ):
            if before:
                await before(env.client)
            results = [
                await env.client.execute_workflow(
                    PreprocessSlateImagesParentWorkflow.run,
                    id=f"{QUEUE}-{uuid.uuid4()}",
                    task_queue=QUEUE,
                )
                for _ in range(firings)
            ]
    return results[-1]


async def test_dispatches_child_with_deterministic_id():
    result = await _run(TARGET, _plan(["a"]))

    assert result == TARGET
    assert f"child:preprocess-slate-{DIVE}" in _EVENTS
    assert _POPULATES == [f"populate-dive-slate-{DIVE}"]


async def test_returns_none_when_no_dive():
    result = await _run(None, None)

    assert result is None
    assert "child" not in _EVENTS
    assert not _POPULATES


async def test_skips_child_when_no_image_checksums():
    result = await _run(TARGET, _plan([]))

    assert result == TARGET
    assert "child" not in _EVENTS
    assert not _POPULATES


async def test_populate_redispatches_on_a_later_firing_for_the_same_dive():
    """Same stall regression as the headtail parent (prod dive 60,
    2026-08-04): a *completed* populate must not burn the child id, or a dive
    that later gains an eligible image never gets a task and never drains."""
    await _run(TARGET, _plan(["a"]), firings=2)

    assert (
        _POPULATES == [f"populate-dive-slate-{DIVE}"] * 2
    ), "the second firing must re-dispatch populate, or the dive can never drain"


async def test_lowers_the_reprocess_flag_even_when_no_work_resolves():
    """A flag that reaches no image must still be lowered -- for the whole
    dive (None), since nothing will ever lower it otherwise and the dive is
    re-selected every hour, ahead of every newer dive."""
    result = await _run(TARGET, _plan([]))

    assert result == TARGET
    assert _CLEAR_SCOPES == [None], "the flag must be lowered on the no-work path"


async def test_the_success_path_clears_only_what_it_redrew_after_populate():
    """Scoped to the frames this run redrew, and after populate: a flag raised
    while the child ran survives, and a populate failure keeps the flags."""
    await _run(TARGET, _plan(["a", "b"]))

    assert _CLEAR_SCOPES == [["a", "b"]]
    assert _EVENTS.index("clear") > _EVENTS.index("populate")


async def test_the_whole_play_in_order():
    """v1's sequence: wake (so the pod's cold start overlaps staging), stage
    the frames and the slate PDF, run the child, clean up, populate, clear."""
    await _run(TARGET, _plan(["a"]))

    assert [e for e in _EVENTS if not e.startswith("child:")] == [
        "wake",
        "stage",
        "stage_pdf",
        "child",
        "cleanup",
        "populate",
        "clear",
    ]


async def test_a_quiet_firing_wakes_nothing():
    """No dive, or a dive with nothing to draw: no pod is stood up for nothing."""
    for selector, plan in ((None, None), (TARGET, _plan([]))):
        _EVENTS.clear()
        await _run(selector, plan)
        assert "wake" not in _EVENTS
        assert "stage" not in _EVENTS


async def test_a_running_child_keeps_its_scratch_and_its_flags():
    """Another firing's child is still reading the raw scratch. Cleaning up
    under it deleted 984 objects on prod dive 442 (2026-09-07), and clearing
    its flags lost the frames it had not redrawn yet."""

    async def a_child_is_running(client):
        await client.start_workflow(
            "PreprocessSlateImagesWorkflow",
            _plan(["a"]).payload,
            id=f"preprocess-slate-{DIVE}",
            task_queue="no-worker-serves-this-queue",
        )

    result = await _run(TARGET, _plan(["a"]), before=a_child_is_running)

    assert result == TARGET
    assert "cleanup" not in _EVENTS
    assert "clear" not in _EVENTS
    assert not _POPULATES


def test_the_child_is_a_registered_raw_reader():
    """The cleanup gate only protects readers it knows: an unregistered child
    id would let another stage's cleanup delete frames under it."""
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )

    assert raw_scratch_reader_id("preprocess-slate", DIVE) == f"preprocess-slate-{DIVE}"
