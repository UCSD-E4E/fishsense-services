"""The laser slice's orchestrator activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
src/fishsense_api_workflow_worker/activities/:
select_next_high_priority_dive_for_laser_{preprocessing,prediction,auto_accept}
_activity.py, resolve_laser_{preprocess,predict}_inputs_activity.py,
clear_laser_reprocess_flags_activity.py, persist_laser_predictions_activity.py,
select_dives_needing_laser_population_activity.py,
create_laser_label_studio_project_activity.py,
populate_laser_label_studio_project_activity.py,
backfill_laser_predictions_activity.py, apply_laser_auto_accept_activity.py,
get_dives_with_complete_laser_labeling_activity.py; and the reads and writes
the v1 data-worker's gate, validator and remediation activities made through
the API (evaluate_laser_auto_accept_activity.py,
validate_laser_labels_for_dive_activity.py, laser_supersede_remediation.py).
Behaviour is v1's; the database side is `fishsense_services_api.laser_store`.

v2 changes:

* the target is (tenant, dive), and a selector takes the oldest candidate
  across every tenant the orchestrator serves;
* the processor is handed `ObjectRef`s -- the staged raw frame, and where its
  JPEG goes (over v1's for a migrated frame) -- and a task's image is the JPEG
  the object store located;
* **the gate, the validator and remediation read and write here**, around the
  processor's decision, because the processor never touches the database. A
  write the store refuses (a row outside the dive) is final, not retried;
* populate records each row's `source` (`human`, or `auto_accept` for a frame
  imported already annotated), and the auto-accept apply marks the rows it
  annotated `auto_accept`: the gate confirmed them (docs/port-plan.md);
* remediation's apply refuses, writing nothing, if the dive's labels changed
  since the plan it applies was made.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import List, Optional, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.laser_store import (
    ForeignRows,
    GateVerdict,
    LaserCandidate,
    LineFit,
    NewLaserPrediction,
    PopulatedLabel,
    PopulationChanged,
)
from fishsense_services_contracts.laser import (
    EvaluateLaserAutoAcceptInput,
    GatePrediction,
    LaserAutoAcceptResult,
    LaserAutoAcceptSummary,
    LaserLabelRow,
    LaserPredictImage,
    LaserPredictionResult,
    LaserPreprocessImage,
    LaserValidationResult,
    PlanLaserRemediationInput,
    PredictLaserImagesInput,
    PreprocessLaserImagesInput,
    ValidateLaserLabelsInput,
    laser_model_version_tag,
)
from fishsense_services_contracts.laser_region import (
    DEFAULT_LASER_BBOX,
    LASER_REGION_POLYGON,
)
from fishsense_services_contracts.object_store import LASER_JPEG_FOLDER
from fishsense_services_orchestrator.labels.label_studio import heartbeat_again
from fishsense_services_orchestrator.labels.populate import (
    TaskImage,
    ensure_project_shows_predictions,
    import_tasks_and_record_labels,
    publish_label_studio_project,
)
from fishsense_services_orchestrator.laser.annotations import (
    LASER_LABELING_CONFIG_XML,
    LASER_PROJECT_TITLE_SUFFIX,
    Dot,
    auto_accepted_annotations,
    build_laser_task,
    dive_laser_label,
    prediction_annotations,
)
from fishsense_services_orchestrator.laser.contracts import (
    ClearReprocessFlags,
    DiveRemediationRequest,
    LaserTarget,
    RemediationInputs,
    RemediationTarget,
    ReviveLabels,
)

__all__ = ["LaserActivities", "LaserCatalog"]


class LaserCatalog(Protocol):
    """What the laser activities ask the database; see
    ``fishsense_services_api.laser_store.LaserCatalog``, whose method names
    these are."""

    async def member_tenants(self) -> list[uuid.UUID]: ...


def _foreign(exc: ForeignRows) -> ApplicationError:
    """The processor named a row outside the dive: final, retrying re-reads
    the same answer to the same conclusion."""
    return ApplicationError(str(exc), type="ForeignRows", non_retryable=True)


def _dot(row) -> Dot:
    return Dot(row.x, row.y, row.width, row.height)


class LaserActivities:
    # pylint: disable=too-many-public-methods
    """The laser slice's activities, given its catalog, the object store and
    Label Studio."""

    def __init__(
        self,
        *,
        catalog,
        store,
        label_studio,
        label_projects,
        bot_user_id: int = 0,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._ls = label_studio
        self._projects = label_projects
        self._bot_user_id = bot_user_id

    # -- selectors ----------------------------------------------------------------

    async def _oldest(self, selector: str, what: str) -> Optional[LaserTarget]:
        best: LaserTarget | None = None
        best_key = None
        for tenant_id in await self._catalog.member_tenants():
            found: LaserCandidate | None = await getattr(self._catalog, selector)(
                tenant_id
            )
            if found is None:
                continue
            key = (found.created_at, found.number)
            if best_key is None or key < best_key:
                best = LaserTarget(tenant_id=tenant_id, dive_id=found.dive_id)
                best_key = key
        if best is None:
            activity.logger.info("no high-priority dives needing %s", what)
        else:
            activity.logger.info(
                "next high-priority dive needing %s: tenant=%s dive=%s",
                what,
                best.tenant_id,
                best.dive_id,
            )
        return best

    async def _every(self, selector: str) -> List[LaserTarget]:
        found = []
        for tenant_id in await self._catalog.member_tenants():
            for candidate in await getattr(self._catalog, selector)(tenant_id):
                found.append((candidate.created_at, candidate.number, tenant_id,
                              candidate.dive_id))  # fmt: skip
        return [
            LaserTarget(tenant_id=tenant, dive_id=dive)
            for _, _, tenant, dive in sorted(found, key=lambda f: (f[0], f[1]))
        ]

    @activity.defn(name="select_next_dive_for_laser_preprocessing")
    async def select_next_dive_for_laser_preprocessing(self) -> Optional[LaserTarget]:
        return await self._oldest(
            "next_dive_for_laser_preprocessing", "laser preprocessing"
        )

    @activity.defn(name="select_next_dive_for_laser_prediction")
    async def select_next_dive_for_laser_prediction(self) -> Optional[LaserTarget]:
        return await self._oldest("next_dive_for_laser_prediction", "laser prediction")

    @activity.defn(name="select_next_dive_for_laser_auto_accept")
    async def select_next_dive_for_laser_auto_accept(self) -> Optional[LaserTarget]:
        return await self._oldest(
            "next_dive_for_laser_auto_accept", "the laser auto-accept gate"
        )

    @activity.defn(name="select_dives_needing_laser_population")
    async def select_dives_needing_laser_population(self) -> List[LaserTarget]:
        """Every dive in the populate cohort, oldest first."""
        return await self._every("dives_needing_laser_population")

    @activity.defn(name="laser_dives_with_complete_labeling")
    async def laser_dives_with_complete_labeling(self) -> List[LaserTarget]:
        """The validation cohort: dives whose laser labeling is complete."""
        return await self._every("dives_with_complete_laser_labeling")

    # -- stage 0.1 ---------------------------------------------------------------

    async def _camera(self, target: LaserTarget):
        # The cohorts select only dives with a camera (laser_store
        # `_HAS_CAMERA`), so this fails only on a calibration removed between
        # the select and the resolve: a one-off, not a dive re-selected hourly.
        camera = await self._catalog.dive_camera(target.tenant_id, target.dive_id)
        if camera is None:
            raise ApplicationError(
                f"dive {target.dive_id} has no camera calibration to rectify with",
                type="DiveHasNoCamera",
                non_retryable=True,
            )
        return camera

    @activity.defn(name="resolve_laser_preprocess_inputs")
    async def resolve_laser_preprocess_inputs(
        self, target: LaserTarget
    ) -> PreprocessLaserImagesInput:
        """The dive's canonical captures needing a laser JPEG (the cohort's
        predicate), each with its staged raw frame and where its JPEG goes."""
        tenant = target.tenant_id
        captures = await self._catalog.laser_preprocess_captures(tenant, target.dive_id)
        images = [
            LaserPreprocessImage(
                capture_id=c.capture_id,
                raw=self._store.layout.raw(tenant, c.checksum),
                jpeg=await self._store.processed_jpeg_target(
                    tenant, LASER_JPEG_FOLDER, c.checksum, from_v1=c.from_v1
                ),
            )
            for c in captures
        ]
        activity.logger.info(
            "resolved laser preprocess inputs dive=%s images=%d",
            target.dive_id,
            len(images),
        )
        if not images:
            # No camera needed for no work; the parent lowers the flags.
            return PreprocessLaserImagesInput(
                dive_id=target.dive_id, images=[],
                camera_matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                distortion_coefficients=[0.0] * 5, bbox=list(DEFAULT_LASER_BBOX),
            )  # fmt: skip
        camera = await self._camera(target)
        return PreprocessLaserImagesInput(
            dive_id=target.dive_id,
            images=images,
            camera_matrix=camera.camera_matrix,
            distortion_coefficients=camera.distortion_coefficients,
            bbox=list(DEFAULT_LASER_BBOX),
            laser_region=[list(v) for v in LASER_REGION_POLYGON],
        )

    @activity.defn(name="clear_laser_reprocess_flags")
    async def clear_laser_reprocess_flags(self, request: ClearReprocessFlags) -> int:
        """Lower `needs_reprocess` once the JPEGs are redrawn: the one cohort
        term that never goes false by itself. Idempotent."""
        target = request.target
        cleared = await self._catalog.clear_laser_reprocess_flags(
            target.tenant_id, target.dive_id, request.capture_ids
        )
        activity.logger.info(
            "cleared laser reprocess flags dive=%s rows=%d scope=%s",
            target.dive_id,
            cleared,
            "whole dive" if request.capture_ids is None else len(request.capture_ids),
        )
        return cleared

    # -- laser prediction -----------------------------------------------------------

    @activity.defn(name="resolve_laser_predict_inputs")
    async def resolve_laser_predict_inputs(
        self, target: LaserTarget
    ) -> PredictLaserImagesInput:
        """The dive's canonical captures whose prediction is missing or stale
        and that no human finished, each with its staged raw frame."""
        tenant = target.tenant_id
        captures = await self._catalog.laser_predict_captures(tenant, target.dive_id)
        activity.logger.info(
            "resolved laser predict inputs dive=%s needing=%d",
            target.dive_id,
            len(captures),
        )
        images = [
            LaserPredictImage(
                capture_id=c.capture_id, raw=self._store.layout.raw(tenant, c.checksum)
            )
            for c in captures
        ]
        if not images:
            return PredictLaserImagesInput(
                dive_id=target.dive_id, images=[],
                camera_matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                distortion_coefficients=[0.0] * 5,
            )  # fmt: skip
        camera = await self._camera(target)
        return PredictLaserImagesInput(
            dive_id=target.dive_id,
            images=images,
            camera_matrix=camera.camera_matrix,
            distortion_coefficients=camera.distortion_coefficients,
            # Colour isn't known before prediction: the model's unknown channel.
            wavelength=None,
            laser_region=[list(v) for v in LASER_REGION_POLYGON],
        )

    @activity.defn(name="persist_laser_predictions")
    async def persist_laser_predictions(
        self, target: LaserTarget, results: List[LaserPredictionResult]
    ) -> int:
        """Append one prediction per result; returns the count written."""
        activity.logger.info("persisting %d laser predictions", len(results))
        try:
            return await self._catalog.persist_laser_predictions(
                target.tenant_id,
                target.dive_id,
                [
                    NewLaserPrediction(
                        capture_id=r.capture_id,
                        confidence=r.confidence,
                        x=r.x,
                        y=r.y,
                        width=r.width,
                        height=r.height,
                        color=r.color,
                        color_margin=r.color_margin,
                        rejected_out_of_region=r.rejected_out_of_region,
                        predictor_version=r.predictor_version,
                        checkpoint=r.checkpoint,
                        core_version=r.core_version,
                    )
                    for r in results
                ],
            )
        except ForeignRows as exc:
            raise _foreign(exc) from exc

    # -- the auto-accept gate -------------------------------------------------------

    @activity.defn(name="resolve_laser_gate_inputs")
    async def resolve_laser_gate_inputs(
        self, target: LaserTarget
    ) -> EvaluateLaserAutoAcceptInput:
        """The dive's WHOLE current prediction set: the gate is a consensus."""
        inputs = await self._catalog.laser_gate_inputs(target.tenant_id, target.dive_id)
        return EvaluateLaserAutoAcceptInput(
            dive_id=target.dive_id,
            dive_number=inputs.dive_number,
            predictions=[
                GatePrediction(
                    prediction_id=p.prediction_id,
                    capture_number=p.capture_number,
                    x=p.x,
                    y=p.y,
                    predictor_version=p.predictor_version,
                )
                for p in inputs.predictions
            ],
        )

    @activity.defn(name="record_laser_gate_verdicts")
    async def record_laser_gate_verdicts(
        self, target: LaserTarget, result: LaserAutoAcceptResult
    ) -> LaserAutoAcceptSummary:
        """Append each verdict that changed; returns the summary with the
        count written (v1 wrote only changed rows too)."""
        try:
            written = await self._catalog.record_laser_gate_verdicts(
                target.tenant_id,
                target.dive_id,
                [
                    GateVerdict(
                        f.prediction_id,
                        f.auto_accept,
                        f.gate_verdict,
                        f.line_offset_px,
                        f.line_position_z,
                    )
                    for f in result.frames
                ],
            )
        except ForeignRows as exc:
            raise _foreign(exc) from exc
        summary = result.summary.model_copy(update={"written": written})
        activity.logger.info(
            "dive=%s auto-accept gate wrote %d/%d verdicts; %d frames may skip "
            "review (%d would have, gate enabled=%s)",
            target.dive_id,
            written,
            len(result.frames),
            summary.auto_accepted,
            summary.verdicts.get("auto_accepted", 0),
            summary.enabled,
        )
        return summary

    # -- Label Studio: create, populate -------------------------------------------

    @activity.defn(name="create_laser_label_studio_project")
    async def create_laser_label_studio_project(self, target: LaserTarget) -> int:
        """The dive's laser project, `{name} #{number} - Laser Calibration
        Labeling`: the registry first, then v1's title search, then create."""
        project_id = await self._projects.ensure_dive_project(
            target.tenant_id,
            target.dive_id,
            "laser",
            suffix=LASER_PROJECT_TITLE_SUFFIX,
            labeling_config_xml=LASER_LABELING_CONFIG_XML,
        )
        activity.logger.info(
            "laser LS project dive=%s project_id=%d", target.dive_id, project_id
        )
        return project_id

    @activity.defn(name="populate_laser_label_studio_project")
    async def populate_laser_label_studio_project(
        self, target: LaserTarget, project_id: int
    ) -> int:
        """Push a task for every predicted canonical capture with no live
        completed label, then record its row. Prediction-gated and JPEG-gated
        (v1's); publishes only a complete task set. Returns rows recorded."""
        tenant = target.tenant_id
        population = await self._catalog.laser_populate_items(tenant, target.dive_id)
        laser_label = dive_laser_label(population.colors)
        activity.logger.info(
            "dive=%s laser colour %s from %d/%d predictions with a reading",
            target.dive_id,
            laser_label,
            sum(1 for c in population.colors if c),
            len(population.colors),
        )
        located = []
        for item in population.items:
            # One HEAD per frame: a big dive's gate outlasts the heartbeat.
            heartbeat_again()
            ref = await self._store.locate_processed_jpeg(
                tenant,
                LASER_JPEG_FOLDER,
                item.capture.checksum,
                from_v1=item.capture.from_v1,
            )
            if ref is None:
                activity.logger.info(
                    "laser JPEG not yet written for capture %d; deferring to a "
                    "later populate run",
                    item.capture.number,
                )
                continue
            located.append((item, ref))

        if not located:
            activity.logger.info(
                "dive %s has no capture needing a laser task", target.dive_id
            )
            # The task set is complete; publish iff the project holds tasks.
            if await self._catalog.dive_has_laser_labels_in_project(
                tenant, target.dive_id, project_id
            ):
                await publish_label_studio_project(self._ls, project_id)
            return 0

        tasks = [
            build_laser_task(
                TaskImage(item.capture.number, ref, item.capture.captured_at),
                _dot(item),
                laser_label,
                auto_accept=item.auto_accept,
                bot_user_id=self._bot_user_id,
            )
            for item, ref in located
        ]

        async def record(pair, task_id: int) -> None:
            item, _ = pair
            source = "auto_accept" if item.auto_accept else "human"
            await self._catalog.record_populated_laser_labels(
                tenant,
                [PopulatedLabel(item.capture.capture_id, project_id, task_id, source)],
            )

        result = await import_tasks_and_record_labels(
            self._ls,
            project_id=project_id,
            tasks=tasks,
            record_label=record,
            items=located,
        )
        if result.complete:
            await publish_label_studio_project(self._ls, project_id)
        return result.recorded

    # -- backfill, and the auto-accept apply -----------------------------------------

    @activity.defn(name="backfill_laser_predictions_for_dive")
    async def backfill_laser_predictions_for_dive(self, target: LaserTarget) -> int:
        """Attach current-version predictions to the dive's open tasks (a
        re-prediction changes nothing a labeler sees otherwise), then point
        each per-dive project's `model_version` at the tier it holds.
        Idempotent by the version tag. Returns the number attached."""
        found = await self._catalog.laser_task_targets(target.tenant_id, target.dive_id)
        if not found.targets:
            activity.logger.info(
                "dive %s: no placeable predictions with attachable LS tasks",
                target.dive_id,
            )
            return 0
        laser_label = dive_laser_label(found.colors)
        tag = laser_model_version_tag()
        already: set[int] = set()
        for project_id in {t.ls_project_id for t in found.targets}:
            for prediction in await self._ls.predictions(project_id):
                if prediction.model_version == tag:
                    already.add(prediction.task_id)

        attached = 0
        tags_by_project: dict[int, Counter] = {}
        for t in found.targets:
            wrapper = prediction_annotations(_dot(t), laser_label)
            if not wrapper:
                continue
            body = wrapper[0]
            tags_by_project.setdefault(t.ls_project_id, Counter())[
                body["model_version"]
            ] += 1
            if t.ls_task_id in already:
                continue
            await self._ls.create_prediction(
                t.ls_task_id, body["model_version"], body["result"]
            )
            attached += 1
            heartbeat_again()

        await ensure_project_shows_predictions(
            self._ls, found.dive_number, tags_by_project, tag
        )
        activity.logger.info(
            "dive %s: attached %d laser predictions to existing LS tasks (%s)",
            target.dive_id,
            attached,
            laser_label,
        )
        return attached

    @activity.defn(name="apply_laser_auto_accept_for_dive")
    async def apply_laser_auto_accept_for_dive(self, target: LaserTarget) -> int:
        """Annotate the dive's open tasks whose predictions the gate cleared --
        only a task Label Studio says nobody started (no annotation, no
        draft): never overwrite a human, never discard work in progress, and
        a second pass finds nothing. Returns the annotations created."""
        found = await self._catalog.laser_task_targets(
            target.tenant_id, target.dive_id, auto_accepted_only=True
        )
        if not found.targets:
            activity.logger.info(
                "dive %s: no auto-accepted predictions with attachable LS tasks",
                target.dive_id,
            )
            return 0
        laser_label = dive_laser_label(found.colors)
        untouched: set[int] = set()
        for project_id in {t.ls_project_id for t in found.targets}:
            untouched |= await self._ls.untouched_task_ids(project_id)

        applied: list[int] = []
        for t in found.targets:
            if t.ls_task_id not in untouched:
                continue
            wrapper = auto_accepted_annotations(
                _dot(t), laser_label, bot_user_id=self._bot_user_id
            )
            if not wrapper:
                continue
            await self._ls.create_annotation(
                t.ls_task_id,
                t.ls_project_id,
                wrapper[0]["result"],
                wrapper[0]["ground_truth"],
            )
            applied.append(t.ls_task_id)
            heartbeat_again()
        if applied:
            await self._catalog.mark_laser_labels_auto_accepted(
                target.tenant_id, applied
            )
        activity.logger.info(
            "dive %s: auto-accepted %d/%d open tasks (%d were already started)",
            target.dive_id,
            len(applied),
            len(found.targets),
            len(found.targets) - len(applied),
        )
        return len(applied)

    # -- per-dive validation -----------------------------------------------------------

    async def _population(self, tenant, dive):
        population = await self._catalog.laser_label_population(tenant, dive)
        rows = [
            LaserLabelRow(
                label_id=r.label_id,
                number=r.number,
                capture_number=r.capture_number,
                x=r.x,
                y=r.y,
                superseded=r.superseded,
                completed=r.completed,
            )
            for r in population.rows
        ]
        return population, rows

    @activity.defn(name="resolve_laser_validation_inputs")
    async def resolve_laser_validation_inputs(
        self, target: LaserTarget
    ) -> ValidateLaserLabelsInput:
        """The dive's FULL population, superseded included (#927), and its
        calibration frames -- read before the fit, so a failure fails the
        run rather than falling back to the tight rule (v1)."""
        population, rows = await self._population(target.tenant_id, target.dive_id)
        activity.logger.info(
            "dive=%s validation inputs: %d positives, %d calibration frames",
            target.dive_id,
            len(rows),
            len(population.calibration_capture_numbers),
        )
        return ValidateLaserLabelsInput(
            dive_id=target.dive_id,
            labels=rows,
            calibration_capture_numbers=population.calibration_capture_numbers,
        )

    @activity.defn(name="apply_laser_validation")
    async def apply_laser_validation(
        self, target: LaserTarget, result: LaserValidationResult
    ) -> int:
        """Write one judgement: supersede its flagged live rows with their
        reasons, append the line if it changed. Returns rows superseded."""
        line = None
        if result.line is not None:
            line = LineFit(**result.line.model_dump())
        try:
            written = await self._catalog.apply_laser_validation(
                target.tenant_id,
                target.dive_id,
                [(s.label_id, s.reason.value) for s in result.supersede],
                line,
            )
        except ForeignRows as exc:
            raise _foreign(exc) from exc
        activity.logger.info(
            "dive=%s validation status=%s superseded %d/%d positive laser labels "
            "(%d flagged in all); line %s",
            target.dive_id,
            result.status,
            written.superseded,
            result.positives,
            result.flagged,
            "appended" if written.line_appended else "unchanged",
        )
        return written.superseded

    # -- remediation --------------------------------------------------------------------

    @activity.defn(name="resolve_laser_remediation_dives")
    async def resolve_laser_remediation_dives(
        self, numbers: Optional[List[int]]
    ) -> List[RemediationTarget]:
        """The dives a run names, by number -- every dive of every served
        tenant when it names none (v1: every dive)."""
        tenants = await self._catalog.member_tenants()
        found: dict[int, RemediationTarget] = {}
        for tenant_id in tenants:
            wanted = numbers
            if wanted is None:
                wanted = await self._catalog.dive_numbers(tenant_id)
            for number in wanted:
                dive = await self._catalog.dive_by_number(tenant_id, number)
                if dive is not None:
                    found[number] = RemediationTarget(
                        tenant_id=tenant_id, dive_id=dive, number=number
                    )
        if numbers is not None and (missing := sorted(set(numbers) - set(found))):
            raise ApplicationError(
                f"no served tenant has dive(s) {missing}",
                type="UnknownDive",
                non_retryable=True,
            )
        return [found[n] for n in sorted(found)]

    @activity.defn(name="resolve_laser_remediation_inputs")
    async def resolve_laser_remediation_inputs(
        self, request: DiveRemediationRequest
    ) -> RemediationInputs:
        """One dive's rows for its plan, and the digest of exactly those rows."""
        target = request.target
        population, rows = await self._population(target.tenant_id, target.dive_id)
        return RemediationInputs(
            plan_input=PlanLaserRemediationInput(
                dive_id=target.number,
                labels=rows,
                calibration_capture_numbers=population.calibration_capture_numbers,
                excluded_label_ids=list(request.excluded_label_ids),
                dive_excluded=request.dive_excluded,
            ),
            fingerprint=population.fingerprint,
        )

    @activity.defn(name="apply_laser_remediation")
    async def apply_laser_remediation(self, request: ReviveLabels) -> int:
        """Revive a reviewed plan's labels on one dive. Trusts nothing it is
        handed: refuses (non-retryable, nothing written) any id the fresh plan
        does not contain, or a dive whose labels changed since that plan."""
        target = request.target
        if unplanned := sorted(set(request.pending) - set(request.planned)):
            raise ApplicationError(
                f"dive {target.number}: refusing to revive {unplanned}; the "
                "current plan does not contain them (excluded, flagged by the "
                "fit, or changed since the dry run). Re-run the dry run and "
                "review it again. Nothing was written.",
                type="RemediationPlanMismatch",
                non_retryable=True,
            )
        if not request.pending:
            return 0
        try:
            written = await self._catalog.revive_laser_labels(
                target.tenant_id, target.dive_id, request.pending, request.fingerprint
            )
        except (PopulationChanged, ForeignRows) as exc:
            raise ApplicationError(
                f"dive {target.number}: {exc}. Re-run the dry run and review it "
                "again. Nothing was written.",
                type="RemediationPlanMismatch",
                non_retryable=True,
            ) from exc
        for number in request.pending:
            activity.logger.info(
                "dive=%d REVIVED laser_label_number=%d -> superseded=False "
                "reason=remediation",
                target.number,
                number,
            )
        return written
