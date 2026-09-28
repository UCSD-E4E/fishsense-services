"""The laser slice's orchestrator workflows.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
src/fishsense_api_workflow_worker/workflows/:
preprocess_laser_images_parent_workflow.py,
predict_laser_images_parent_workflow.py,
evaluate_laser_auto_accept_parent_workflow.py,
populate_laser_label_studio_project_{parent_,}workflow.py (+ _populate.py
`create_then_populate`), create_laser_label_studio_project_workflow.py,
backfill_laser_predictions_workflow.py,
remediate_laser_supersedes_parent_workflow.py, and the per-dive shape of the
data-worker's validate_laser_labels_for_dive_workflow.py and
remediate_laser_supersedes_workflow.py. The shared steps are v1's _dispatch.py.

v1's invariants, kept:

* each hourly parent drains one dive (populate: every dive in its cohort);
  its schedule skips on overlap; child ids are deterministic
  (`preprocess-laser-{dive}`, `predict-laser-{dive}`,
  `auto-accept-laser-{dive}`, `populate-laser-{dive}`) with ALLOW_DUPLICATE;
* **a child already running under another firing means do nothing further**
  -- no cleanup, no flag clear, no verdicts: that firing owns them (prod dive
  442 lost 984 raw objects and 515 flags otherwise);
* the predict parent returns BEFORE staging when nothing can serve the GPU
  queue; cleanup comes before any Label Studio step (an LS outage once leaked
  1,094 raw objects); the gate and its apply come last;
* the stage-0.1 flag clear is scoped to what was redrawn; the no-work path
  clears the whole dive;
* the processor is woken only once a parent knows there is work.

v2 changes:

* **the gate, the validator and remediation are read, judged and written in
  three steps** -- an orchestrator activity reads the rows, a light-role
  processor child decides, an orchestrator activity writes -- because the
  processor never touches the database (v1's data-worker called the API from
  its activities). The drain's run timeout is still v1's 1 h: the child's
  budget gave up the minutes the read and the write need;
* per-dive validation and remediation are orchestrator children of their own
  (`ValidateDiveLaserLabelsWorkflow`, `RemediateDiveLaserSupersedesWorkflow`),
  so a dive's rows cross a workflow's history only once rather than piling
  into one parent's.
"""

import asyncio
from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from fishsense_services_orchestrator.labels.populate_policy import (
        CREATE_PROJECT_RETRY,
        POPULATE_RETRY,
    )
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_LIGHT_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.laser import (
        GATE_CHILD_EXECUTION_TIMEOUT,
        DivePlan,
        EvaluateLaserAutoAcceptInput,
        LaserAutoAcceptResult,
        LaserAutoAcceptSummary,
        LaserPredictionResult,
        LaserValidationResult,
        PredictLaserImagesInput,
        PreprocessLaserImagesInput,
        RemediateLaserSupersedesInput,
        ValidateLaserLabelsInput,
        revival_digest,
    )
    from fishsense_services_orchestrator.laser.contracts import (
        ClearReprocessFlags,
        DiveRemediation,
        DiveRemediationRequest,
        LaserTarget,
        RemediationInputs,
        RemediationTarget,
        ReviveLabels,
    )
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_UNAVAILABLE
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_gpu_processor,
        wake_light_processor,
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import StagingTarget
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )

__all__ = [
    "BackfillLaserPredictionsWorkflow",
    "CreateLaserLabelStudioProjectWorkflow",
    "EvaluateLaserAutoAcceptParentWorkflow",
    "PopulateLaserLabelStudioProjectParentWorkflow",
    "PopulateLaserLabelStudioProjectWorkflow",
    "PredictLaserImagesParentWorkflow",
    "PreprocessLaserImagesParentWorkflow",
    "RemediateDiveLaserSupersedesWorkflow",
    "RemediateLaserSupersedesParentWorkflow",
    "ValidateDiveLaserLabelsWorkflow",
]

#: v1's `SDK_FAIL_FAST_RETRY_POLICY` for database round trips: one retry for a
#: blip, then fail -- a consistent error is a bug. A lost membership, a row the
#: store refused, a dive with no camera are final.
DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "ForeignRows", "DiveHasNoCamera"],
)

#: v1's selector and resolver budget (`_dispatch.select_dive`/`resolve_inputs`).
SELECT_TIMEOUT = timedelta(minutes=5)
#: v1's `run_sdk_activity` budget, for the Label Studio steps.
LABEL_STUDIO_TIMEOUT = timedelta(minutes=15)
#: The gate's read and write, moved out of v1's data-worker activity. Sized so
#: the drain's worst case still fits v1's deployed 1 h run timeout:
#: 5 (select) + 2.5 + 5 (wake) + 30 (child) + 2.5 + 15 (apply) = 60 min.
GATE_READ_TIMEOUT = timedelta(minutes=2, seconds=30)
GATE_WRITE_TIMEOUT = timedelta(minutes=2, seconds=30)
#: The validator's read and write around its processor child.
VALIDATION_IO_TIMEOUT = timedelta(minutes=5)

#: Bounds on concurrent children, v1's.
POPULATE_CONCURRENCY = 4
REMEDIATION_BATCH = 8


def _staging(target: LaserTarget) -> StagingTarget:
    return StagingTarget(target.tenant_id, target.dive_id)


async def _db(name: str, *args, result_type=None, timeout=SELECT_TIMEOUT):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=timeout,
        retry_policy=DB_FAIL_FAST,
        result_type=result_type,
    )


async def _label_studio(name: str, *args, result_type=None):
    return await workflow.execute_activity(
        name,
        args=args,
        schedule_to_close_timeout=LABEL_STUDIO_TIMEOUT,
        retry_policy=DB_FAIL_FAST,
        result_type=result_type,
    )


async def _child(
    name, arg, *, child_id, task_queue, execution_timeout, result_type=None
):
    """v1's `dispatch_child`: ALLOW_DUPLICATE, and None when the same id is
    already running under another firing -- which then owns everything after
    it."""
    try:
        return True, await workflow.execute_child_workflow(
            name,
            arg,
            id=child_id,
            task_queue=task_queue,
            execution_timeout=execution_timeout,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=result_type,
        )
    except WorkflowAlreadyStartedError:
        workflow.logger.info(
            "%s is already running under another firing; leaving it be", child_id
        )
        return False, None


# -- the gate, shared by the predict parent and the drain -----------------------


async def _gate(target: LaserTarget) -> Optional[LaserAutoAcceptSummary]:
    """Judge the dive's predictions on the light processor, record what
    changed, and apply the cleared verdicts to tasks that already exist. None
    when the other parent is judging this dive right now."""
    await wake_light_processor()
    payload: EvaluateLaserAutoAcceptInput = await _db(
        "resolve_laser_gate_inputs",
        target,
        result_type=EvaluateLaserAutoAcceptInput,
        timeout=GATE_READ_TIMEOUT,
    )
    ran, result = await _child(
        "EvaluateLaserAutoAcceptWorkflow",
        payload,
        # Shared with the other parent, so a dive is never judged twice at once.
        child_id=f"auto-accept-laser-{target.dive_id}",
        task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
        # Outlasts the activity's own budget, so the activity's timeout fires.
        execution_timeout=GATE_CHILD_EXECUTION_TIMEOUT,
        result_type=LaserAutoAcceptResult,
    )
    if not ran:
        return None
    summary: LaserAutoAcceptSummary = await _db(
        "record_laser_gate_verdicts",
        target,
        result,
        result_type=LaserAutoAcceptSummary,
        timeout=GATE_WRITE_TIMEOUT,
    )
    # The per-dive verdict mix is the stage's monitoring signal: watch both
    # tails (v1).
    workflow.logger.info(
        "auto-accept gate dive=%s enabled=%s eligible=%s reason=%s "
        "auto_accepted=%d/%d verdicts=%s written=%d",
        target.dive_id,
        summary.enabled,
        summary.eligible,
        summary.reason,
        summary.auto_accepted,
        sum(summary.verdicts.values()),
        summary.verdicts,
        summary.written,
    )
    if summary.auto_accepted:
        # Populate imports a task once, so a verdict on a dive whose tasks
        # exist changes nothing a labeler sees without this.
        applied = await _label_studio(
            "apply_laser_auto_accept_for_dive", target, result_type=int
        )
        workflow.logger.info(
            "auto-accept applied to existing tasks dive=%s applied=%s",
            target.dive_id,
            applied,
        )
    return summary


# -- stage 0.1 --------------------------------------------------------------------


@workflow.defn
class PreprocessLaserImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive needing laser JPEGs and dispatch its
    redraw to the per-image processor. Returns the target, or None."""

    @workflow.run
    async def run(self) -> Optional[LaserTarget]:
        target: Optional[LaserTarget] = await _db(
            "select_next_dive_for_laser_preprocessing",
            result_type=Optional[LaserTarget],
        )
        if target is None:
            return None
        inputs: PreprocessLaserImagesInput = await _db(
            "resolve_laser_preprocess_inputs",
            target,
            result_type=PreprocessLaserImagesInput,
        )
        workflow.logger.info(
            "dispatching laser preprocess dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            # A flag that reached no image still has to come down: it is the one
            # cohort term that never goes false by itself (v1).
            workflow.logger.warning(
                "reprocess flag resolved to no work; lowering it dive=%s",
                target.dive_id,
            )
            await _db("clear_laser_reprocess_flags", ClearReprocessFlags(target=target))
            return target

        await wake_per_image_processor()
        await stage_raw(_staging(target))
        ran, _ = await _child(
            "PreprocessLaserImagesWorkflow",
            inputs,
            child_id=raw_scratch_reader_id("preprocess-laser", target.dive_id),
            task_queue=PROCESSOR_TASK_QUEUE,
            execution_timeout=timedelta(hours=1),
        )
        if not ran:
            # The running firing reads the scratch this one would delete, and
            # clears the flags for the frames it actually redrew.
            return target
        await cleanup_raw(_staging(target))
        # Scoped to what this run redrew: a flag raised meanwhile survives.
        await _db(
            "clear_laser_reprocess_flags",
            ClearReprocessFlags(
                target=target, capture_ids=[i.capture_id for i in inputs.images]
            ),
        )
        return target


# -- laser prediction ---------------------------------------------------------------


@workflow.defn
class PredictLaserImagesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Pick the oldest high-priority dive needing laser predictions, run the
    detector on the GPU role, persist, backfill, and gate. Returns the target,
    or None (no dive, or nothing can serve the GPU queue)."""

    @workflow.run
    async def run(self) -> Optional[LaserTarget]:
        target: Optional[LaserTarget] = await _db(
            "select_next_dive_for_laser_prediction", result_type=Optional[LaserTarget]
        )
        if target is None:
            return None
        inputs: PredictLaserImagesInput = await _db(
            "resolve_laser_predict_inputs", target, result_type=PredictLaserImagesInput
        )
        workflow.logger.info(
            "dispatching laser predict dive=%s images=%d",
            target.dive_id,
            len(inputs.images),
        )
        if not inputs.images:
            return target

        mode = await wake_gpu_processor()
        if mode == MODE_UNAVAILABLE:
            # Bail BEFORE staging: a child on an unserved queue does not fail,
            # it hangs until its execution timeout. The dive stays in the cohort.
            workflow.logger.warning(
                "no worker available for the laser-predict queue; skipping dive=%s",
                target.dive_id,
            )
            return None
        workflow.logger.info("laser predict running on %s capacity", mode)
        await stage_raw(_staging(target))
        ran, results = await _child(
            "PredictLaserImagesWorkflow",
            inputs,
            child_id=raw_scratch_reader_id("predict-laser", target.dive_id),
            task_queue=PROCESSOR_GPU_TASK_QUEUE,
            # Covers the CPU fallback, far slower per image.
            execution_timeout=timedelta(hours=6),
            result_type=List[LaserPredictionResult],
        )
        if not ran:
            return target

        if results:
            await _db(
                "persist_laser_predictions",
                target,
                results,
                result_type=int,
                timeout=LABEL_STUDIO_TIMEOUT,
            )
        # Before any Label Studio step, deliberately (v1: an LS outage leaked
        # 1,094 staged objects when this came later).
        await cleanup_raw(_staging(target))
        if results:
            # Populate seeds a task's pre-annotation once: attaching to existing
            # tasks is what makes a new prediction visible.
            await _label_studio(
                "backfill_laser_predictions_for_dive", target, result_type=int
            )
            await _gate(target)
        return target


# -- the gate's backlog drain -------------------------------------------------------


@workflow.defn
class EvaluateLaserAutoAcceptParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Drain the gate's backlog, one dive per firing: a dive already fully
    predicted never re-enters the predict cohort, so its predictions would
    otherwise never be judged (v1: 3,711 predictions across ~65 dives)."""

    @workflow.run
    async def run(self) -> Optional[LaserTarget]:
        target: Optional[LaserTarget] = await _db(
            "select_next_dive_for_laser_auto_accept", result_type=Optional[LaserTarget]
        )
        if target is None:
            return None
        await _gate(target)
        return target


# -- Label Studio -----------------------------------------------------------------


@workflow.defn
class CreateLaserLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """On demand: the dive's laser project, found or created. Returns its id."""

    @workflow.run
    async def run(self, target: LaserTarget) -> int:
        return await workflow.execute_activity(
            "create_laser_label_studio_project",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=CREATE_PROJECT_RETRY,
            result_type=int,
        )


@workflow.defn
class PopulateLaserLabelStudioProjectWorkflow:
    # pylint: disable=too-few-public-methods
    """Create the dive's laser project, then push its tasks (v1's
    `create_then_populate`). Returns the rows recorded."""

    @workflow.run
    async def run(self, target: LaserTarget) -> int:
        project_id = await workflow.execute_activity(
            "create_laser_label_studio_project",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=CREATE_PROJECT_RETRY,
            result_type=int,
        )
        return await workflow.execute_activity(
            "populate_laser_label_studio_project",
            args=(target, project_id),
            schedule_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            # Bounded: a retry reconciles (the IMPORT_ISSUED heartbeat), never
            # re-imports; unbounded is what left dive 424 23 copies of a frame.
            retry_policy=POPULATE_RETRY,
            result_type=int,
        )


@workflow.defn
class PopulateLaserLabelStudioProjectParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Fan populate out across every dive in the (prediction-gated) cohort.
    One dive's failure does not abort the others. Returns the dives."""

    @workflow.run
    async def run(self) -> List[LaserTarget]:
        targets: List[LaserTarget] = await _db(
            "select_dives_needing_laser_population", result_type=List[LaserTarget]
        )
        if not targets:
            return []
        sem = asyncio.Semaphore(POPULATE_CONCURRENCY)

        async def _populate(target: LaserTarget) -> None:
            async with sem:
                await _child(
                    "PopulateLaserLabelStudioProjectWorkflow",
                    target,
                    child_id=f"populate-laser-{target.dive_id}",
                    task_queue=workflow.info().task_queue,
                    execution_timeout=timedelta(minutes=30),
                )

        outcomes = await asyncio.gather(
            *(_populate(t) for t in targets), return_exceptions=True
        )
        for target, outcome in zip(targets, outcomes):
            if isinstance(outcome, BaseException):
                workflow.logger.error(
                    "laser populate failed for dive=%s: %s", target.dive_id, outcome
                )
        return targets


@workflow.defn
class BackfillLaserPredictionsWorkflow:
    # pylint: disable=too-few-public-methods
    """On demand: attach a dive's predictions to its existing tasks (for a dive
    predicted before the backfill ran). Idempotent. Returns the number
    attached."""

    @workflow.run
    async def run(self, target: LaserTarget) -> int:
        return await _label_studio(
            "backfill_laser_predictions_for_dive", target, result_type=int
        )


# -- per-dive validation -------------------------------------------------------------


@workflow.defn
class ValidateDiveLaserLabelsWorkflow:
    # pylint: disable=too-few-public-methods
    """One dive's laser-label validation: read its full population, judge it
    once on the light processor, write the supersedes and the line. Started
    by the laser-label sync (`validate-laser-labels-{dive}`, v1's id). Returns
    the rows superseded."""

    @workflow.run
    async def run(self, target: LaserTarget) -> int:
        payload: ValidateLaserLabelsInput = await _db(
            "resolve_laser_validation_inputs",
            target,
            result_type=ValidateLaserLabelsInput,
            timeout=VALIDATION_IO_TIMEOUT,
        )
        ran, result = await _child(
            "ValidateLaserLabelsForDiveWorkflow",
            payload,
            child_id=f"judge-laser-labels-{target.dive_id}",
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
            # v1's: at least the activity's 15 minutes.
            execution_timeout=timedelta(minutes=20),
            result_type=LaserValidationResult,
        )
        if not ran:
            return 0
        return await _db(
            "apply_laser_validation",
            target,
            result,
            result_type=int,
            timeout=VALIDATION_IO_TIMEOUT,
        )


# -- remediation ------------------------------------------------------------------


@workflow.defn
class RemediateDiveLaserSupersedesWorkflow:
    # pylint: disable=too-few-public-methods
    """Plan one dive -- and, given reviewed `revive_ids`, apply them: re-plan
    from current state and refuse any id the fresh plan does not contain
    (v1's apply activity), writing nothing."""

    @workflow.run
    async def run(self, request: DiveRemediationRequest) -> DiveRemediation:
        inputs: RemediationInputs = await _db(
            "resolve_laser_remediation_inputs",
            request,
            result_type=RemediationInputs,
            timeout=VALIDATION_IO_TIMEOUT,
        )
        _, plan = await _child(
            "PlanLaserSupersedeRemediationWorkflow",
            inputs.plan_input,
            child_id=f"{workflow.info().workflow_id}-judge",
            task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
            execution_timeout=timedelta(minutes=20),
            result_type=DivePlan,
        )
        if not request.revive_ids:
            return DiveRemediation(plan=plan)
        live = {r.number for r in inputs.plan_input.labels if not r.superseded}
        written = await workflow.execute_activity(
            "apply_laser_remediation",
            ReviveLabels(
                target=request.target,
                # Ids already live are skipped, so re-applying is a no-op.
                pending=[i for i in request.revive_ids if i not in live],
                planned=plan.revive_ids,
                fingerprint=inputs.fingerprint,
            ),
            schedule_to_close_timeout=VALIDATION_IO_TIMEOUT,
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["NotAMember", "RemediationPlanMismatch"],
            ),
            result_type=int,
        )
        return DiveRemediation(plan=plan, written=written)


@workflow.defn(name="RemediateLaserSupersedesParentWorkflow")
class RemediateLaserSupersedesParentWorkflow:
    # pylint: disable=too-few-public-methods
    """On demand, from the operator CLI: plan every named dive (dry run), or
    apply a reviewed plan. Dry run unless `apply` AND the plan recomputed now
    has the reviewed report's digest; an empty plan is a clean no-op, which is
    what makes re-applying safe. Returns the report (v1's shape)."""

    @workflow.run
    async def run(self, request: RemediateLaserSupersedesInput) -> dict:
        targets: List[RemediationTarget] = await _db(
            "resolve_laser_remediation_dives",
            None if request.all_dives else sorted(set(request.dive_ids)),
            result_type=List[RemediationTarget],
        )
        # The light processor is torn down when idle, and a child on an
        # unserved queue hangs rather than fails.
        await wake_light_processor()
        excluded = set(request.excluded_dive_ids)
        me = workflow.info().workflow_id

        async def _dive(target: RemediationTarget, revive_ids, step: str):
            return await workflow.execute_child_workflow(
                "RemediateDiveLaserSupersedesWorkflow",
                DiveRemediationRequest(
                    target=target,
                    excluded_label_ids=list(request.excluded_label_ids),
                    dive_excluded=target.number in excluded,
                    revive_ids=list(revive_ids),
                ),
                id=f"{me}-{step}-{target.number}",
                execution_timeout=timedelta(hours=1),
                result_type=DiveRemediation,
            )

        rows: list[dict] = []
        for start in range(0, len(targets), REMEDIATION_BATCH):
            batch = targets[start : start + REMEDIATION_BATCH]
            outcomes = await asyncio.gather(*(_dive(t, [], "plan") for t in batch))
            rows += [outcome.plan.to_dict() for outcome in outcomes]

        digest = revival_digest((row["dive_id"], row["revive_ids"]) for row in rows)
        report = {
            "mode": "dry_run",
            "plan_sha256": digest,
            "excluded_dive_ids": sorted(excluded),
            "excluded_label_ids": sorted(set(request.excluded_label_ids)),
            "totals": {
                "dives": len(rows),
                "positives": sum(row["positives"] for row in rows),
                "superseded_now": sum(row["superseded_now"] for row in rows),
                "superseded_after": sum(row["superseded_after"] for row in rows),
                "to_revive": sum(len(row["revive_ids"]) for row in rows),
                "reflection_suspects": sum(
                    1 for row in rows if row["reflection_suspect"] is not None
                ),
            },
            "dives": rows,
            "applied": None,
        }
        if not request.apply:
            return report

        by_number = {t.number: t for t in targets}
        to_apply = [row for row in rows if row["revive_ids"]]
        if not to_apply:
            report["mode"] = "apply_noop"
            report["applied"] = {}
            return report
        if request.expected_plan_sha256 != digest:
            raise ApplicationError(
                f"refusing to apply: the plan now has digest {digest}, the "
                f"reviewed report has {request.expected_plan_sha256}. Re-run the "
                "dry run and review it again. Nothing was written.",
                type="RemediationPlanMismatch",
                non_retryable=True,
            )
        applied: dict[str, int] = {}
        for start in range(0, len(to_apply), REMEDIATION_BATCH):
            batch = to_apply[start : start + REMEDIATION_BATCH]
            outcomes = await asyncio.gather(
                *(
                    _dive(by_number[row["dive_id"]], row["revive_ids"], "apply")
                    for row in batch
                )
            )
            applied.update(
                {str(row["dive_id"]): o.written for row, o in zip(batch, outcomes)}
            )
        report["mode"] = "apply"
        report["applied"] = applied
        return report
