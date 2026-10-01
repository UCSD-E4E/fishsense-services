"""The calibration parents: stage 13 (slate), the checkerboard, and the
lattice study.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/workflows/ (perform_laser_calibration_parent_
workflow.py, perform_checkerboard_calibration_parent_workflow.py,
verify_checkerboard_lattice_parent_workflow.py, and the _dispatch.py steps
they used).

**Stage 13** is selector -> child: pure maths over already-stored slate and
laser labels, on the light queue, with no staging. **The checkerboard** has to
put the frames where the processor can read them, because the board is only
visible in pixels: selector -> resolver -> wake -> stage -> wake -> child ->
clean up, on the per-image queue. **The lattice study** is operator-driven
(never scheduled): it renders the frames a set of calibrations were fitted
from, one dive at a time, and imports them all into one shuffled project.

v1's rules, kept:

* the schedule skips on overlap and each child's id is deterministic, so two
  firings never fit one dive; ids are reused with ALLOW_DUPLICATE, so a dive
  whose bad calibration was remediated can be refitted, and a child still
  *running* under that id is left to the run that owns it;
* the checkerboard parent **wakes the processor twice**, before and after
  staging: staging runs ~30 minutes on the orchestrator's queue, during which
  the processor queue looks idle to the sweeper;
* it cleans up the raw scratch **even when the child fails** -- "no board in
  these frames" is this stage's expected outcome -- but never under a child
  another run owns (prod dive 442);
* the study isolates each dive (one unusable dive must not discard an hour
  of staging), shuffles the renders with `workflow.random()` (replay-safe)
  *before* importing them in chunks of 100 (Temporal's 2 MB payload limit),
  so no chunk is one dive's block.

v2 changes:

* **the parent records the result.** v1's data-worker PUT the extrinsics or
  recorded the refusal itself; the processor returns a
  `LaserCalibrationResult` and the parent appends it, with the provenance its
  resolver read, to `laser_calibrations`. A refusal is recorded, then raised
  non-retryably under v1's error type so the run still fails loud; a failure
  to record never masks it, and an accepted fit that cannot be recorded
  fails the run. The record is retried, so it is named by the run: a retry
  after a lost reply appends nothing;
* stage 13 resolves its inputs before waking anything (v1's child read them),
  so a dive with nothing to fit wakes no pod;
* targets are (tenant, dive); the children run on the processor's queues,
  raw-reading ones under ids from `raw_scratch_reader_id`; the wakes stand
  the processor up (`nrp`);
* the study takes a tenant slug and dive numbers (v1's dive ids).
"""

from datetime import timedelta
from typing import List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_LIGHT_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.slate_calibration import (
        CheckerboardLatticeRender,
        LaserCalibrationResult,
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
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_light_processor,
        wake_per_image_processor,
    )
    from fishsense_services_orchestrator.object_store.contracts import (
        StagingTarget,
    )
    from fishsense_services_orchestrator.object_store.readers import (
        raw_scratch_reader_id,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )

__all__ = [
    "IMPORT_CHUNK_SIZE",
    "PerformCheckerboardCalibrationParentWorkflow",
    "PerformLaserCalibrationParentWorkflow",
    "VerifyCheckerboardLatticeParentWorkflow",
]

# Selectors and resolvers are database round trips: one retry for a blip,
# then fail (v1's SDK_FAIL_FAST). A lost membership, or a dive that cannot be
# resolved, is final.
_DB_FAIL_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=2,
    non_retryable_error_types=["NotAMember", "CalibrationInputsUnavailable"],
)

#: Renders per import call. A 10x14 render is ~2.6-6 KB, so 100 is well under
#: Temporal's 2 MB payload limit, and a retried chunk re-imports nothing
#: (dedup by URL).
IMPORT_CHUNK_SIZE = 100


async def _select(name: str) -> Optional[StagingTarget]:
    return await workflow.execute_activity(
        name,
        schedule_to_close_timeout=timedelta(minutes=5),
        retry_policy=_DB_FAIL_FAST,
        result_type=Optional[StagingTarget],
    )


async def _record_and_judge(
    target: StagingTarget,
    result: LaserCalibrationResult,
    provenance: CalibrationProvenance,
) -> None:
    """Append the result; then, for a refusal, fail the run under its type."""
    try:
        await workflow.execute_activity(
            "record_laser_calibration",
            RecordCalibration(
                tenant_id=target.tenant_id,
                dive_id=target.dive_id,
                result=result,
                provenance=provenance,
            ),
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["NotAMember"]
            ),
        )
    except ActivityError as exc:
        if result.outcome == "accepted":
            raise
        # Best-effort for a refusal: losing the real refusal (and its
        # non-retryable type) to a recording failure would be worse (v1).
        workflow.logger.error(
            "could not record the calibration refusal for dive=%s: %s",
            target.dive_id,
            exc,
        )
    if result.outcome == "refused":
        raise ApplicationError(
            f"dive_id={target.dive_id}: {result.refusal_reason}",
            type=result.refusal_type,
            non_retryable=True,
        )
    workflow.logger.info(
        "calibrated dive=%s producer=%s observations=%d",
        target.dive_id,
        provenance.producer,
        result.observation_count,
    )


@workflow.defn
class PerformLaserCalibrationParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Fit the laser of the next HIGH dive stage 13 can calibrate from its
    slate. Returns the target, or None when the cohort is empty. Each firing
    drains one dive."""

    @workflow.run
    async def run(self) -> Optional[StagingTarget]:
        target = await _select("select_next_dive_for_laser_calibration")
        if target is None:
            return None

        plan: Optional[SlateCalibrationPlan] = await workflow.execute_activity(
            "resolve_slate_calibration_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[SlateCalibrationPlan],
        )
        if plan is None:
            workflow.logger.info(
                "dive=%s has no slate labels to calibrate from", target.dive_id
            )
            return target

        await wake_light_processor()
        try:
            result: LaserCalibrationResult = await workflow.execute_child_workflow(
                "PerformLaserCalibrationWorkflow",
                plan.payload,
                id=f"perform-laser-calibration-{target.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                execution_timeout=timedelta(minutes=15),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=LaserCalibrationResult,
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info(
                "perform-laser-calibration-%s is still running; skipping "
                "duplicate dispatch",
                target.dive_id,
            )
            return target

        await _record_and_judge(target, result, plan.provenance)
        return target


@workflow.defn
class PerformCheckerboardCalibrationParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Fit the laser of the next HIGH dive linked to a checkerboard. Returns the
    target, or None when the cohort is empty. Each firing drains one dive."""

    @workflow.run
    async def run(self) -> Optional[StagingTarget]:
        target = await _select("select_next_dive_for_checkerboard_calibration")
        if target is None:
            return None

        plan: CheckerboardCalibrationPlan = await workflow.execute_activity(
            "resolve_checkerboard_calibration_inputs",
            target,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=CheckerboardCalibrationPlan,
        )
        workflow.logger.info(
            "dispatching checkerboard calibration dive=%s frames=%d board=%dx%d",
            target.dive_id,
            len(plan.payload.images),
            plan.payload.target.rows,
            plan.payload.target.cols,
        )
        if not plan.payload.images:
            # The cohort counted at least two dotted frames, so none means the
            # resolver and the selector disagree: find out without staging.
            workflow.logger.warning(
                "checkerboard calibration resolved no frames; not staging dive=%s",
                target.dive_id,
            )
            return target

        # Woken twice: before staging, so the cold start overlaps it; after,
        # because staging leaves the processor queue idle for the sweeper.
        await wake_per_image_processor()
        await stage_raw(target)
        await wake_per_image_processor()

        owns_scratch = True
        try:
            result: LaserCalibrationResult = await workflow.execute_child_workflow(
                "PerformCheckerboardCalibrationWorkflow",
                plan.payload,
                id=raw_scratch_reader_id(
                    "perform-checkerboard-calibration", target.dive_id
                ),
                task_queue=PROCESSOR_TASK_QUEUE,
                # Generous: a rawpy decode and a corner search per frame, two
                # at a time, over as many as 133 frames.
                execution_timeout=timedelta(hours=2),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=LaserCalibrationResult,
            )
        except WorkflowAlreadyStartedError:
            owns_scratch = False
            workflow.logger.info(
                "dive=%s already has a calibration child running; leaving its "
                "raw bytes alone",
                target.dive_id,
            )
            return target
        finally:
            # Even when the child failed: that is the expected shape of "these
            # frames hold no detectable board", and the scratch is reproducible.
            if owns_scratch:
                await cleanup_raw(target)

        await _record_and_judge(target, result, plan.provenance)
        return target


@workflow.defn
class VerifyCheckerboardLatticeParentWorkflow:
    # pylint: disable=too-few-public-methods
    """Render each dive's lattices, then import them all as one shuffled
    project. Returns the number of Label Studio tasks imported. Run by hand::

        temporal workflow start --task-queue fishsense_orchestrator \\
            --type VerifyCheckerboardLatticeParentWorkflow \\
            --workflow-id verify-lattice-<run-tag> \\
            --input '{"tenant": "lab", "dives": [493, 495, 496], "sample_limit": 20}'
    """

    @workflow.run
    async def run(self, payload: VerifyCheckerboardLatticeParentInput) -> int:
        tenant_id = await workflow.execute_activity(
            "resolve_lattice_tenant",
            payload.tenant,
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
        )

        renders: List[CheckerboardLatticeRender] = []
        failed: List[int] = []
        for number in payload.dives:
            # Per-dive isolation: without it one unusable dive discards every
            # dive rendered before it, with all of their staging.
            try:
                renders.extend(
                    await self._render_dive(tenant_id, number, payload.sample_limit)
                )
            except Exception as exc:  # pylint: disable=broad-except
                failed.append(number)
                workflow.logger.error(
                    "lattice verification failed for dive %d: %s", number, exc
                )
        if failed:
            workflow.logger.error(
                "lattice verification skipped %d of %d dives: %s",
                len(failed),
                len(payload.dives),
                failed,
            )
        if not renders:
            workflow.logger.warning("lattice verification produced no renders")
            return 0

        # The shuffle keeps the study blind: Label Studio serves tasks in
        # import order. `workflow.random()` is replay-safe.
        workflow.random().shuffle(renders)

        project_id: int = await workflow.execute_activity(
            "create_checkerboard_lattice_label_studio_project",
            LatticeProject(tenant_id=tenant_id, tenant_slug=payload.tenant),
            schedule_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(non_retryable_error_types=["NotAMember"]),
            result_type=int,
        )

        imported = 0
        for start in range(0, len(renders), IMPORT_CHUNK_SIZE):
            imported += await workflow.execute_activity(
                "populate_checkerboard_lattice_label_studio_project",
                LatticeImport(
                    tenant_id=tenant_id,
                    ls_project_id=project_id,
                    renders=renders[start : start + IMPORT_CHUNK_SIZE],
                ),
                schedule_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(non_retryable_error_types=["NotAMember"]),
                result_type=int,
            )

        workflow.logger.info(
            "lattice verification imported %d tasks from %d of %d dives",
            imported,
            len(payload.dives) - len(failed),
            len(payload.dives),
        )
        return imported

    async def _render_dive(
        self, tenant_id, number: int, sample_limit
    ) -> List[CheckerboardLatticeRender]:
        """Stage one dive's frames, render its lattices, drop the scratch."""
        plan: Optional[LatticePlan] = await workflow.execute_activity(
            "resolve_lattice_inputs",
            LatticeDive(tenant_id=tenant_id, number=number, sample_limit=sample_limit),
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DB_FAIL_FAST,
            result_type=Optional[LatticePlan],
        )
        if plan is None or not plan.payload.images:
            workflow.logger.warning(
                "lattice verification resolved no frames; not staging dive %d",
                number,
            )
            return []

        target = plan.target
        await wake_per_image_processor()
        await stage_raw(target)
        await wake_per_image_processor()

        owns_scratch = True
        try:
            return await workflow.execute_child_workflow(
                "VerifyCheckerboardLatticeWorkflow",
                plan.payload,
                id=raw_scratch_reader_id("verify-checkerboard-lattice", target.dive_id),
                task_queue=PROCESSOR_TASK_QUEUE,
                execution_timeout=timedelta(hours=2),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                result_type=List[CheckerboardLatticeRender],
            )
        except WorkflowAlreadyStartedError:
            owns_scratch = False
            workflow.logger.info(
                "dive %d already has a lattice child running; skipping", number
            )
            return []
        finally:
            if owns_scratch:
                await cleanup_raw(target)
