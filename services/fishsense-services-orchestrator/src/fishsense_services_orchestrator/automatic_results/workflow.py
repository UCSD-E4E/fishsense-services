"""The automatic-results workflows (orchestrator side).

New in v2. One backlog dive per run, every step of cscw-fishsense2027@96a8da07
PAPER.md §6's chain, each on the processor role that owns it:

1. **frames** (GPU): stage the raw frames; render the captures owing one
   (the render stage's steps, per-image, on this staging) before waking the
   GPU; then per frame the detector's dot, the SAM 3.1 mask at it (score >=
   0.5) in the frame's render and its head/tail, `FRAMES_PER_CHILD` frames a
   child; persist; clean up. A frame still without a render is left owed;
2. **calibration** (per-image): the label-free size-constancy fit on the
   dive's slate frames; with none, the refusal is written without a processor;
3. **lengths** (light): at the automatic dot's depth, under the calibration
   the dive's lengths use now;
4. **species** (GPU): BioCLIP zero-shot on each kept mask, through the species
   stage's own processor workflow (`PredictSpeciesImagesWorkflow`). **Last,
   and its failure is logged, not raised**: species never costs a dive its
   lengths. (2026-10-15 audit: it ran second, with BioCLIP unprovisioned, and
   its failure failed every dive before a length was written.)

Rules kept from the other parents (`species_predict.workflow`,
`laser.workflow`): deterministic child ids reused ALLOW_DUPLICATE, and a child
still running under another firing means its step is skipped; the GPU is woken
only once there is GPU work; raw staging before the child and cleanup after,
the child named a raw-scratch reader. And one of its own: **only a real GPU
runs the GPU steps** -- the CPU fallback would run Mask R-CNN, whose lengths
were never validated, so on `cpu_fallback` or `unavailable` those steps wait
for a later run and the CPU steps go on with what is there.

`AutomaticResultsParentWorkflow` is the schedule's: it **drains** the
backlog, dive after dive, until `AUTOMATIC_DRAIN_WINDOW` has passed since it
started, the cohort is empty, it is handed a dive it already took (it
tells the cohort which), or a dive's GPU step was skipped (with no GPU every
dive behind it would skip too). Each run returns an `AutomaticRunReport`:
the dives it took, what each skipped or failed, and why it stopped -- a GPU
skip is said, not silent (2026-10-15 gap audit). **A dive owing frames goes
no further that run** (its frames skipped for want of a GPU, left out by the
processor, or running under another firing): its calibration and lengths
would be fit on part of it and go stale, and each hour without a GPU added a
refusal to its history (review of #85). A frame left out is recorded against
the frame, never as a dive strike; three runs make it a terminal
``frame_failed`` (0044), and the dive goes on. **A processor's refusal is
the worker's, not the dive's** (2026-10-24): a pod with no GPU for SAM 3.1,
or whose weights cannot load (`fishsense_services_contracts.refusals`), fails
the frames child with that cause. Recorded as the dive's failure, each dive
the drain went on to took a strike for the pod's fault (at three the cohort
leaves it for an operator); now nothing is recorded against the dive or its
frames, the scratch is cleaned up, the refusal is logged as an error and
named in the report (`refused`), and the run stops there.

**Replay.** The reports (2026-10-16/17) changed these workflows' commands
and results without `workflow.patched` guards: on 2026-10-17 production had
never run either (0 executions, no `automatic-results` schedule), so there
was nothing in flight to replay. Once the track runs, guard such changes. **A dive's failed step does not hold the backlog**
(2026-10-15 audit: the oldest dive, failing deterministically, was selected
every hour and starved the rest): the failure is recorded against the dive
(at three the cohort leaves it for an operator), its raw scratch is cleaned
up, and the run goes on. `AutomaticResultsForMissionWorkflow` runs a named dive
on demand, as the validation harness does for the paper's dives, which are
not in the backlog; it records a failure too, and then fails, so a dive run
by hand never reads as done when it isn't.
"""

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Awaitable, Callable, List, Optional, TypeVar, Annotated

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    from fishsense_services_contracts.renamed import renamed_from
    from fishsense_services_contracts.refusals import WORKER_REFUSALS
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_LIGHT_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.automatic_results import (
        AUTOMATIC_CALIBRATION_VERSION,
        AutomaticCalibrationResult,
        AutomaticFrameResult,
        MeasureAutomaticResult,
        PredictAutomaticFramesInput,
    )
    from fishsense_services_contracts.calibration_v3 import (
        CalibrationV3Report,
        ReplayCalibrationInput,
    )
    from fishsense_services_contracts.headtail_keypoints import (
        KeypointFramesInput,
        KeypointResult,
    )
    from fishsense_services_contracts.species_prediction import (
        SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
        PredictSpeciesImagesInput,
        SpeciesPredictionResult,
    )
    from fishsense_services_orchestrator.automatic_results.activities import (
        AutomaticCalibrationPlan,
        AutomaticMeasurePlan,
        AutomaticTarget,
    )
    from fishsense_services_orchestrator.calibration_v3.workflow import (
        pose_slate_frames,
    )

    # The slate pose's PDF staging argument, built here as in the replay's
    # sandbox (a dataclass the sandbox re-imports is not fully defined).
    from fishsense_services_orchestrator.slates.contracts import (  # noqa: F401
        SlatePdfTarget,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.nrp.workflow import (
        GPU_WAKE_TIMEOUT,
        WAKE_TIMEOUT,
        wake_gpu_processor,
        wake_light_processor,
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import StagingTarget
    from fishsense_services_orchestrator.renders.activities import RenderTarget
    from fishsense_services_orchestrator.renders.workflow import render_pages
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_contracts.masks import MaskUpload
    from fishsense_services_orchestrator.object_store.steps import (
        LONG_STAGE_RAW_TIMEOUT,
        cleanup_raw,
        stage_raw,
    )

T = TypeVar("T")

__all__ = [
    "AUTOMATIC_DRAIN_WINDOW",
    "AutomaticMissionReport",
    "AutomaticResultsForMissionWorkflow",
    "AutomaticResultsParentWorkflow",
    "AutomaticRunReport",
    "GPU_CHILD_MAX",
    "GPU_CHILD_MIN",
    "RUN_TIMEOUT",
    "gpu_child_timeout",
]


@dataclass
class AutomaticMissionReport:
    """What a run did to one dive. A step skipped for want of a GPU is named
    in `skipped` -- the dive still owes it, and a run that read as a quiet
    success hid that (2026-10-15 gap audit). `failed` is the step whose
    failure was recorded against the dive; `species_failed`, a species
    failure (logged, never recorded: the dive keeps its lengths)."""

    tenant_id: uuid.UUID
    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]
    skipped: List[str] = field(default_factory=list)
    failed: Optional[str] = None
    species_failed: bool = False
    #: The GPU's mode when a GPU step was skipped for want of one: then
    #: every dive behind this one would skip too, and the run stops.
    no_gpu: Optional[str] = None
    #: A processor's refusal of a step (``<step>: <type>: <message>``): the
    #: worker's fault, recorded against nothing; every dive behind this one
    #: would be refused too, and the run stops.
    refused: Optional[str] = None
    #: That refusal's type (e.g. ``Sam3WeightsCorrupt``), as data: the drain
    #: and a by-hand run name it without parsing `refused` (review of #364).
    refusal_type: Optional[str] = None


@dataclass
class AutomaticRunReport:
    """A backlog run: the dives it took, in order, and why it stopped --
    ``backlog empty``, ``dive handed back``, ``drain window``, or ``no GPU
    (<mode>)``."""

    missions: Annotated[List[AutomaticMissionReport], renamed_from("dives", "missions")]
    stopped: str


_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "InvalidPredictions"],
)
DB_TIMEOUT = timedelta(minutes=15)
#: A GPU child's budget, by how many frames (or images) it was given: it
#: returns only at the end, so a budget that runs out loses all of it and the
#: next firing starts over. ~60 s a frame is three times the measured 18 s
#: (SAM 3.1, one frame at a time on the GPU, 2026-10-18), for queueing behind
#: other GPU work; the largest dives have ~1,700 frames (review of #110).
GPU_CHILD_MIN = timedelta(hours=6)
GPU_CHILD_MAX = timedelta(hours=36)
GPU_CHILD_PER_FRAME = timedelta(seconds=60)


def gpu_child_timeout(frames: int) -> timedelta:
    budget = timedelta(hours=1) + frames * GPU_CHILD_PER_FRAME
    return min(max(budget, GPU_CHILD_MIN), GPU_CHILD_MAX)


CPU_CHILD_TIMEOUT = timedelta(hours=1)
#: Above the processor's fit budget (FIT_TIMEOUT, 2 h), so a slow fit is not
#: cut off by its parent.
CALIBRATION_CHILD_TIMEOUT = timedelta(hours=2, minutes=15)
STAGE_TIMEOUT = LONG_STAGE_RAW_TIMEOUT
#: How long after its start a run may begin another dive (the hourly schedule
#: skips a firing while a run is going).
AUTOMATIC_DRAIN_WINDOW = timedelta(minutes=50)
#: The schedule's run timeout: the window, then one dive's every step at its
#: longest (two GPU children, two CPU ones, staging and cleanup, three wakes,
#: ten database steps).
#:
#: **Changing it changes a schedule.** Schedules are created if missing and
#: never updated in place (CLAUDE.md), so a live ``automatic-results`` schedule
#: keeps the run timeout it was created with: delete it in Temporal just before
#: the deploy that changes this, and the restarted worker recreates it (review
#: of #219; on 2026-10-21 the schedule was off, so nothing held the old one).
RUN_TIMEOUT = (
    AUTOMATIC_DRAIN_WINDOW
    + 10 * DB_TIMEOUT
    + GPU_WAKE_TIMEOUT
    + 2 * WAKE_TIMEOUT
    + 2 * GPU_CHILD_MAX
    + CALIBRATION_CHILD_TIMEOUT
    + CPU_CHILD_TIMEOUT
    + 2 * STAGE_TIMEOUT
)


async def _db(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=DB_TIMEOUT,
        retry_policy=_DB_FAIL_FAST,
        result_type=result_type,
    )


async def _child(name, arg, *, child_id, task_queue, timeout, result_type):
    """(ran, result): not ran when the same id is running under another firing,
    which then owns its step."""
    try:
        return True, await workflow.execute_child_workflow(
            name,
            arg,
            id=child_id,
            task_queue=task_queue,
            execution_timeout=timeout,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=result_type,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info("%s is running under another firing", child_id)
        return False, None


class _Gpu:
    """The GPU, woken at most once per run, and only when there is GPU work."""

    def __init__(self) -> None:
        self.mode: Optional[str] = None

    async def ready(self) -> bool:
        if self.mode is None:
            self.mode = await wake_gpu_processor()
        if self.mode != MODE_GPU:
            workflow.logger.warning(
                "automatic results need SAM 3.1 on a GPU; got %s, skipping the "
                "GPU steps this run",
                self.mode,
            )
        return self.mode == MODE_GPU


def _no_gpu(step: str, gpu: _Gpu) -> str:
    return f"{step}: no GPU ({gpu.mode})"


def _skip_for_gpu(step: str, gpu: _Gpu, report: AutomaticMissionReport) -> None:
    report.skipped.append(_no_gpu(step, gpu))
    report.no_gpu = gpu.mode


#: What waits while a dive owes frames: fit or measured on part of its
#: frames, they would go stale as the rest arrive (review of #85).
_AFTER_FRAMES = "calibration, lengths, species"


#: The change that stores each frame's SAM mask (contract 12, 2026-10-21): a
#: run started before it replays without the presign step.
MASKS_PATCH = "automatic-frames-store-masks"

#: Frames per GPU child: each carries its render's presigned GET, and a whole
#: large mission's would pass Temporal's 2 MB payload limit (review of #144).
FRAMES_PER_CHILD = 400


async def _frames(
    target: AutomaticTarget, gpu: _Gpu, report: AutomaticMissionReport
) -> bool:
    """The dive's owed frames; True when none is owed after this run."""
    inputs: PredictAutomaticFramesInput = await _db(
        "resolve_automatic_frames_inputs",
        target,
        result_type=PredictAutomaticFramesInput,
    )
    if not inputs.frames:
        return True
    staging = StagingTarget(target.tenant_id, target.mission_id)
    # The scratch is this firing's to clean up, whatever happens -- unless
    # the child is running under another firing, which owns it.
    staged, owns_scratch = False, True
    try:
        if any(f.render_url is None for f in inputs.frames):
            # SAM segments each frame's render: make the missing ones first,
            # on the per-image role and on this staging (one NAS pass), before
            # the GPU is woken -- it would sit idle through them (review of
            # #144) -- then read them back by URL.
            staged = True
            await stage_raw(staging, timeout=STAGE_TIMEOUT)
            rendered = await render_pages(
                RenderTarget(target.tenant_id, target.mission_id)
            )
            if rendered.no_bucket:
                report.skipped.append("frames: the tenant has no bucket for renders")
            inputs = await _db(
                "resolve_automatic_frames_inputs",
                target,
                result_type=PredictAutomaticFramesInput,
            )
        ready = [f for f in inputs.frames if f.render_url is not None]
        unrendered = len(inputs.frames) - len(ready)
        if unrendered:
            report.skipped.append(
                f"{_AFTER_FRAMES}: {unrendered} frames have no render yet"
            )
        if not ready:
            return False
        if not await gpu.ready():
            _skip_for_gpu("frames", gpu, report)
            return False
        if not staged:
            # Every frame rendered: the dot detector reads the owed frames'
            # raws, and only those are staged (review of #219).
            staged = True
            await stage_raw(
                StagingTarget(
                    target.tenant_id,
                    target.mission_id,
                    capture_ids=[f.capture_id for f in ready],
                ),
                timeout=STAGE_TIMEOUT,
            )
        missing: List[uuid.UUID] = []
        for start in range(0, len(ready), FRAMES_PER_CHILD):
            chunk = ready[start : start + FRAMES_PER_CHILD]
            if workflow.patched(MASKS_PATCH):
                # Contract 12: each frame's mask upload, presigned for this
                # chunk (a whole mission's URLs would pass the 2 MB limit).
                uploads = await _db(
                    "presign_automatic_masks",
                    target,
                    [f.capture_id for f in chunk],
                    result_type=List[Optional[MaskUpload]],
                )
                chunk = [
                    f.model_copy(update={"mask": u}) for f, u in zip(chunk, uploads)
                ]
            ran, results = await _child(
                "PredictAutomaticFramesWorkflow",
                inputs.model_copy(update={"frames": chunk}),
                child_id=raw_scratch_reader_id("automatic-frames", target.mission_id),
                task_queue=PROCESSOR_GPU_TASK_QUEUE,
                timeout=gpu_child_timeout(len(chunk)),
                result_type=List[AutomaticFrameResult],
            )
            if not ran:
                owns_scratch = False
                report.skipped.append(
                    f"{_AFTER_FRAMES}: frames running under another firing"
                )
                return False
            if results:
                await _db("persist_automatic_frames", target, results, result_type=int)
            returned = {r.capture_id for r in results or []}
            missing += [f.capture_id for f in chunk if f.capture_id not in returned]
        # The processor leaves out a frame that failed every try, so one bad
        # frame no longer costs the dive the rest (2026-10-15). What was left
        # out is recorded against each *frame*, never as one of the dive's
        # strikes, which a transient fault would spend; at three runs the
        # store makes it a terminal abstention (review of #85).
        if missing:
            given_up = await _db(
                "record_automatic_frame_failures",
                target,
                missing,
                "left out after the processor's tries (its log has the cause)",
                result_type=List[uuid.UUID],
            )
            owed = len(missing) - len(given_up)
            workflow.logger.warning(
                "automatic frames for dive=%s: %d of %d left out, %d given up",
                target.mission_id, len(missing), len(ready), len(given_up),
            )  # fmt: skip
            if owed:
                plural = "frame" if owed == 1 else "frames"
                report.skipped.append(f"{_AFTER_FRAMES}: {owed} {plural} still owed")
                return False
        return not unrendered
    finally:
        if staged and owns_scratch:
            await cleanup_raw(staging)


#: Automatic head/tail v5 (owner, 2026-10-24/25): SimCC keypoints on each
#: non-model frame's SAM box, between the frames and the calibration, and the
#: lengths' refusals for frames sent to review. A run started before it
#: replays without either.
KEYPOINTS_PATCH = "automatic-headtail-v5-keypoints"


async def _keypoints(
    target: AutomaticTarget, gpu: _Gpu, report: AutomaticMissionReport
) -> None:
    """SimCC on the frames owed keypoints (the processor's keypoint workflow,
    the replay's, #329); only a real GPU runs it, as the frames."""
    inputs: KeypointFramesInput = await _db(
        "resolve_automatic_keypoint_inputs",
        target,
        result_type=KeypointFramesInput,
    )
    if not inputs.frames:
        return
    if not await gpu.ready():
        _skip_for_gpu("keypoints", gpu, report)
        return
    ran, results = await _child(
        "PredictHeadtailKeypointsWorkflow",
        inputs,
        child_id=f"automatic-keypoints-{target.mission_id}",
        task_queue=PROCESSOR_GPU_TASK_QUEUE,
        timeout=gpu_child_timeout(len(inputs.frames)),
        result_type=List[KeypointResult],
    )
    if ran and results:
        await _db("persist_automatic_keypoints", target, results, result_type=int)


async def _species(
    target: AutomaticTarget, gpu: _Gpu, report: AutomaticMissionReport
) -> None:
    """Species, isolated: a failure (BioCLIP unprovisioned, a GPU out of
    memory) is logged and the dive keeps its lengths; species work stays
    outstanding, so a later run retries it."""
    try:
        await _species_step(target, gpu, report)
    except (ActivityError, ChildWorkflowError) as exc:
        refusal = _refusal(exc)
        if refusal is not None and workflow.patched(SPECIES_REFUSAL_PATCH):
            # The worker's, as any step's refusal: every dive behind this one
            # would wake the GPU to be refused alike (review of #364). The
            # dive keeps its lengths; nothing is recorded against it.
            workflow.logger.error(
                "automatic species for dive=%s refused by the processor: %s: %s",
                target.mission_id, refusal.type, refusal.message,
            )  # fmt: skip
            report.refused = f"species: {refusal.type}: {refusal.message}"
            report.refusal_type = refusal.type
            return
        report.species_failed = True
        workflow.logger.warning(
            "automatic species failed for dive=%s; lengths are kept: %s",
            target.mission_id,
            exc.cause or exc,
        )


async def _species_step(
    target: AutomaticTarget, gpu: _Gpu, report: AutomaticMissionReport
) -> None:
    inputs: PredictSpeciesImagesInput = await _db(
        "resolve_automatic_species_inputs",
        target,
        result_type=PredictSpeciesImagesInput,
    )
    if not inputs.images:
        return
    if not await gpu.ready():
        _skip_for_gpu("species", gpu, report)
        return
    ran, results = await _child(
        "PredictSpeciesImagesWorkflow",
        inputs,
        child_id=f"automatic-species-{target.mission_id}",
        task_queue=PROCESSOR_GPU_TASK_QUEUE,
        timeout=gpu_child_timeout(len(inputs.images)),
        result_type=List[SpeciesPredictionResult],
    )
    persistable = [
        r for r in results or [] if r.status != SPECIES_STATUS_NO_UPGRADE_AVAILABLE
    ]
    if ran and persistable:
        await _db("persist_automatic_species", target, persistable, result_type=int)


async def _calibration(target: AutomaticTarget) -> None:
    plan: AutomaticCalibrationPlan = await _db(
        "resolve_automatic_calibration_inputs",
        target,
        result_type=AutomaticCalibrationPlan,
    )
    if not plan.payload.frames:
        result = AutomaticCalibrationResult(
            mission_id=target.mission_id,
            outcome="refused",
            refusal_reason="no_candidates",
            algorithm_version=AUTOMATIC_CALIBRATION_VERSION,
        )
    else:
        await wake_per_image_processor()
        ran, result = await _child(
            "FitAutomaticCalibrationWorkflow",
            plan.payload,
            child_id=f"automatic-calibration-{target.mission_id}",
            task_queue=PROCESSOR_TASK_QUEUE,
            timeout=CALIBRATION_CHILD_TIMEOUT,
            result_type=AutomaticCalibrationResult,
        )
        if not ran:
            return
    await _db("persist_automatic_calibration", target, plan, result)
    if workflow.patched(SLATE_POSE_PATCH):
        await _slate_pose(target, plan)


#: The slate-pose calibration (calibration v4, 0078; the owner, 2026-10-26),
#: after the size-constancy fit on the same slate frames: only where it is
#: owed (its switch on, a template, not current). A run started before it
#: replays without it.
SLATE_POSE_PATCH = "automatic-slate-pose-calibration"
#: Every slate-pose answer carries its error bar (0079; 2026-10-27): one
#: without, from a processor before the relaxed fit, is not written.
SLATE_POSE_ERROR_BAR_PATCH = "automatic-slate-pose-error-bar"


async def _slate_pose(target: AutomaticTarget, plan: AutomaticCalibrationPlan) -> None:
    """Each slate frame posed from the mission's template on a real GPU, the
    beam fitted through the posed dots (the replay's fit, on the per-image
    role), written as the slate pose. Without a real GPU nothing is written:
    it is owed still."""
    if not await _db("automatic_slate_pose_owed", target, result_type=bool):
        return
    template: Optional[dict] = await _db(
        "resolve_mission_slate_template", target, result_type=Optional[dict]
    )
    if template is None:
        return
    slate_template_id = uuid.UUID(template["slate_template_id"])
    if not plan.payload.frames:
        report = CalibrationV3Report(mission_id=target.mission_id, size_source="pose",
                                     outcome="refused",
                                     refusal_reason="no_candidates")  # fmt: skip
        await _db("persist_automatic_slate_pose", target, plan, report,
                  slate_template_id, [])  # fmt: skip
        return
    poses = await pose_slate_frames(
        target.tenant_id, target.mission_id, plan, template,
        child_prefix=f"automatic-slate-pose-{target.mission_id}",
    )  # fmt: skip
    if poses is None:
        workflow.logger.warning(
            "dive=%s: the slate pose needs a real GPU; owed still",
            target.mission_id,
        )
        return
    # Every pose is kept; only a strong one is fitted (its status says so).
    points = {str(p.capture_id): p.point if p.status == "posed" else None
              for p in poses}  # fmt: skip
    await wake_per_image_processor()
    ran, report = await _child(
        "ReplayAutomaticCalibrationV3Workflow",
        ReplayCalibrationInput(payload=plan.payload, size_source="pose",
                               pose_points=points),
        child_id=f"automatic-slate-pose-fit-{target.mission_id}",
        task_queue=PROCESSOR_TASK_QUEUE,
        timeout=CALIBRATION_CHILD_TIMEOUT,
        result_type=CalibrationV3Report,
    )  # fmt: skip
    if not ran:
        return
    if (report.outcome == "accepted" and report.scale_sd is None
            and workflow.patched(SLATE_POSE_ERROR_BAR_PATCH)):  # fmt: skip
        workflow.logger.warning(
            "dive=%s: the slate pose came back without an error bar (a processor "
            "from before the relaxed fit); owed still", target.mission_id,
        )  # fmt: skip
        return
    posed = [p.capture_id for p in poses if p.status == "posed"]
    await _db("persist_automatic_slate_pose", target, plan, report,
              slate_template_id, posed)  # fmt: skip


async def _lengths(target: AutomaticTarget) -> None:
    plan: Optional[AutomaticMeasurePlan] = await _db(
        "resolve_automatic_measure_inputs",
        target,
        result_type=Optional[AutomaticMeasurePlan],
    )
    if plan is None:
        return
    if plan.refusals and workflow.patched(KEYPOINTS_PATCH):
        await _db("persist_automatic_measure_refusals", target, plan, result_type=int)
    if not plan.payload.captures:
        return
    await wake_light_processor()
    ran, result = await _child(
        "MeasureAutomaticWorkflow",
        plan.payload,
        child_id=f"automatic-measure-{target.mission_id}",
        task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
        timeout=CPU_CHILD_TIMEOUT,
        result_type=MeasureAutomaticResult,
    )
    if ran:
        await _db(
            "persist_automatic_measurements", target, plan, result, result_type=int
        )


class StepFailed(Exception):
    """A dive's step failed; it is recorded against the dive."""


class StepRefused(Exception):
    """A processor refused a dive's step; recorded against nothing."""

    def __init__(self, step: str, refusal: ApplicationError) -> None:
        super().__init__(f"{step}: {refusal.type}: {refusal.message}")
        self.refusal_type = refusal.type


#: The change that stopped a processor's refusal costing a dive a strike
#: (2026-10-24): a run started before it replays recording the failure.
REFUSAL_PATCH = "automatic-worker-refusal-not-a-strike"
#: The change that made a species refusal stop the run like any step's
#: (review of #364): a run started before it replays logging it as a species
#: failure and going on.
SPECIES_REFUSAL_PATCH = "automatic-species-refusal-stops"


def _refusal(exc: BaseException) -> Optional[ApplicationError]:
    """The processor's refusal behind a failed step, if that is what it was:
    a child's failure wraps its activity's, so the whole chain is read."""
    cause: Optional[BaseException] = exc
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type in WORKER_REFUSALS:
            return cause
        cause = getattr(cause, "cause", None)
    return None


async def _step(
    target: AutomaticTarget, name: str, run: Callable[[], Awaitable[T]]
) -> T:
    try:
        return await run()
    except (ActivityError, ChildWorkflowError) as exc:
        refusal = _refusal(exc)
        if refusal is not None and workflow.patched(REFUSAL_PATCH):
            workflow.logger.error(
                "automatic %s for dive=%s refused by the processor, not "
                "recorded against it: %s: %s",
                name, target.mission_id, refusal.type, refusal.message,
            )  # fmt: skip
            raise StepRefused(name, refusal) from exc
        error = str(exc.cause or exc)
        workflow.logger.warning(
            "automatic %s failed for dive=%s: %s", name, target.mission_id, error
        )
        await _db("record_automatic_results_failure", target, name, error)
        raise StepFailed(name) from exc


async def _run_for_mission(
    target: AutomaticTarget,
) -> tuple[AutomaticMissionReport, _Gpu]:
    """Every step; a failed one is recorded and ends the dive (`failed`)."""
    gpu = _Gpu()
    report = AutomaticMissionReport(target.tenant_id, target.mission_id)
    try:
        if not await _step(target, "frames", lambda: _frames(target, gpu, report)):
            return report, gpu
        if workflow.patched(KEYPOINTS_PATCH):
            await _step(target, "keypoints", lambda: _keypoints(target, gpu, report))
        await _step(target, "calibration", lambda: _calibration(target))
        await _step(target, "lengths", lambda: _lengths(target))
    except StepFailed as exc:
        report.failed = str(exc)
        return report, gpu
    except StepRefused as exc:
        report.refused = str(exc)
        report.refusal_type = exc.refusal_type
        return report, gpu
    # Isolated: a species failure is logged, and the dive keeps its lengths.
    await _species(target, gpu, report)
    if report.skipped:
        workflow.logger.warning(
            "automatic results for dive=%s skipped: %s",
            target.mission_id,
            "; ".join(report.skipped),
        )
    return report, gpu


@workflow.defn(name="AutomaticResultsForDiveWorkflow")
class AutomaticResultsForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    """Every automatic step for one named dive (on demand)::

        temporal workflow start --task-queue fishsense_orchestrator \\
            --type AutomaticResultsForDiveWorkflow \\
            --workflow-id automatic-results-<dive uuid> \\
            --input '{"tenant_id": "<uuid>", "dive_id": "<uuid>"}'
    """

    @workflow.run
    async def run(self, target: AutomaticTarget) -> AutomaticMissionReport:
        report, _ = await _run_for_mission(target)
        if report.refused:
            raise ApplicationError(
                f"automatic {report.refused} (dive {target.mission_id}; the "
                "processor's refusal, recorded against nothing)",
                report.refusal_type,  # details: the type, for `run_missions`
                type="AutomaticWorkerRefused",
                non_retryable=True,
            )
        if report.failed:
            raise ApplicationError(
                f"automatic {report.failed} failed for dive {target.mission_id} "
                "(recorded)",
                type="AutomaticStepFailed",
                non_retryable=True,
            )
        return report


@workflow.defn
class AutomaticResultsParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Drains the backlog, oldest dive first across tenants, every step of
    each, and reports each dive and why it stopped. With no GPU it stops at
    the first dive: every backlog dive owes GPU work (its frames) first."""

    @workflow.run
    async def run(self) -> AutomaticRunReport:
        started = workflow.now()
        taken: List[uuid.UUID] = []
        missions: List[AutomaticMissionReport] = []
        stopped = "drain window"
        while workflow.now() - started < AUTOMATIC_DRAIN_WINDOW:
            target: Optional[AutomaticTarget] = await _db(
                "select_next_dive_for_automatic_results",
                list(taken),
                result_type=Optional[AutomaticTarget],
            )
            if target is None:
                stopped = "backlog empty"
                break
            if target.mission_id in taken:
                stopped = "dive handed back"
                break
            taken.append(target.mission_id)
            report, gpu = await _run_for_mission(target)
            missions.append(report)
            if report.no_gpu:
                stopped = f"no GPU ({report.no_gpu})"
                break
            if report.refused:
                # Every dive behind it would be refused alike.
                stopped = f"worker refused ({report.refusal_type})"
                break
        return AutomaticRunReport(missions=missions, stopped=stopped)
