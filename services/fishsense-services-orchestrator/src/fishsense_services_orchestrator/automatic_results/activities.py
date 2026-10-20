"""The automatic-results track's orchestrator activities.

New in v2: fish lengths with no human label (cscw-fishsense2027@96a8da07
PAPER.md §6), as their own track (decided 2026-10-06). Built as head/tail
prediction's and species prediction's activities are, and keeping their
rules:

* the target is (tenant, dive); the selector takes the oldest backlog dive
  across every tenant the orchestrator serves;
* **the orchestrator issues the keys** (PLAN.md §9.11): a frame's staged raw,
  and its automatic render's (`object_store.automatic_jpeg_folder`: the render
  version and the camera, under the tenant);
* **one image per frame, rectified from the raw** (2026-10-19): the frames
  stage renders it from the raw with the camera's own matrix and distortion
  -- the dot's pixel space -- finds the mask against it and writes it, for
  every frame, under its render version (`AUTOMATIC_JPEG_RENDER_VERSION`,
  recorded on the frame's row); species and calibration read the render the
  row records, and a frame with none recorded waits. They used to read the
  head/tail JPEG, which for a migrated frame is v1's render: not the image the
  dot and mask were found in. That JPEG is the one human labels were drawn on,
  so this track never writes it;
* species reuse the species stage's processor contract unchanged, cropped by
  the automatic mask's box, with the automatic head/tail's id in the
  `headtail_prediction_id` slot (it is echoed back, and the write checks it is
  the capture's own); a JPEG not yet in Garage defers the fish;
* a length is written with the calibration it was computed under, carried
  from the resolve (`AutomaticMeasurePlan`); a newer calibration then makes it
  stale by itself (`current_automatic_measurements`);
* a refusal of the processor's output is final (non-retryable).
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Annotated

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.automatic_results_store import (
    AutomaticCalibrationInputs,
    AutomaticCalibrationRow,
    AutomaticCandidate,
    AutomaticFramesInputs,
    AutomaticHeadTailRow,
    AutomaticMeasureInputs,
    AutomaticMeasurementRow,
    AutomaticSpeciesCapture,
    AutomaticSpeciesRow,
    InvalidAutomaticResults,
)
from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_CALIBRATION_VERSION,
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AUTOMATIC_JPEG_RENDER_VERSION,
    AUTOMATIC_HEADTAIL_STATUSES,
    AUTOMATIC_MEASUREMENT_VERSION,
    AutomaticCalibrationFrame,
    AutomaticCalibrationResult,
    AutomaticFrame,
    AutomaticFrameResult,
    AutomaticMeasureCapture,
    FitAutomaticCalibrationInput,
    MeasureAutomaticInput,
    MeasureAutomaticResult,
    PredictAutomaticFramesInput,
)
from fishsense_services_contracts.laser_region import LASER_REGION_POLYGON
from fishsense_services_contracts.object_store import ObjectRef, automatic_jpeg_folder
from fishsense_services_contracts.species_prediction import (
    SPECIES_PREDICTOR_VERSION,
    SPECIES_STATUSES,
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesPredictionResult,
)
from fishsense_services_orchestrator.species_predict.candidates import (
    species_candidates,
)
from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "AutomaticCalibrationPlan",
    "AutomaticMeasurePlan",
    "AutomaticResultsActivities",
    "AutomaticResultsCatalog",
    "AutomaticTarget",
    "VERSIONS",
]

#: The stages' current versions, which the cohort selects on a mismatch with.
VERSIONS = {
    "headtail_version": AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    "species_version": SPECIES_PREDICTOR_VERSION,
    "calibration_version": AUTOMATIC_CALIBRATION_VERSION,
    "measurement_version": AUTOMATIC_MEASUREMENT_VERSION,
}


#: The frame statuses with no render: the raw was not there, or would not decode.
_NO_RENDER = frozenset({"raw_unavailable", "decode_failed"})


@dataclass(frozen=True)
class AutomaticTarget:
    """A dive of a tenant."""

    tenant_id: uuid.UUID
    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]


@dataclass(frozen=True)
class AutomaticCalibrationPlan:
    """The frames to fit, and the camera calibration whose intrinsics they use."""

    payload: FitAutomaticCalibrationInput
    camera_calibration_id: Optional[uuid.UUID]


@dataclass(frozen=True)
class AutomaticMeasurePlan:
    """The lengths to compute, and the calibration they are computed under."""

    payload: MeasureAutomaticInput
    calibration_source: str
    automatic_laser_calibration_id: Optional[uuid.UUID]
    laser_calibration_id: Optional[uuid.UUID]
    camera_calibration_id: Optional[uuid.UUID]


class AutomaticResultsCatalog(Protocol):
    """See ``fishsense_services_api.automatic_results_store``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_mission_for_automatic_results(
        self, tenant_id: uuid.UUID, **versions
    ) -> AutomaticCandidate | None: ...

    async def automatic_frames_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, headtail_version: int
    ) -> AutomaticFramesInputs: ...

    async def persist_automatic_head_tails(
        self, tenant_id, mission_id, rows
    ) -> int: ...

    async def automatic_species_captures(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, species_version: int
    ) -> list[AutomaticSpeciesCapture]: ...

    async def persist_automatic_species(self, tenant_id, mission_id, rows) -> int: ...

    async def automatic_calibration_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> AutomaticCalibrationInputs: ...

    async def persist_automatic_calibration(
        self, tenant_id, mission_id, row: AutomaticCalibrationRow
    ) -> uuid.UUID: ...

    async def automatic_measure_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, algorithm_version: str
    ) -> AutomaticMeasureInputs: ...

    async def persist_automatic_measurements(
        self, tenant_id, mission_id, rows
    ) -> int: ...


def _refuse(target: AutomaticTarget, why: str) -> ApplicationError:
    return ApplicationError(
        f"refusing the processor's automatic results for dive {target.mission_id}: {why}",
        type="InvalidPredictions",
        non_retryable=True,
    )


class AutomaticResultsActivities:
    def __init__(
        self,
        *,
        catalog: AutomaticResultsCatalog,
        store: Any,
        species_enabled: bool = True,
    ) -> None:
        self._catalog = catalog
        self._store = store
        #: Species' own switch (2026-10-15): off, no species images, and the
        #: cohort holds no dive for species work.
        self._species_enabled = species_enabled

    def _automatic_jpeg_key(
        self, target: AutomaticTarget, checksum: str, camera: uuid.UUID
    ):
        return self._store.layout.processed_jpeg(
            target.tenant_id,
            automatic_jpeg_folder(AUTOMATIC_JPEG_RENDER_VERSION, camera),
            checksum,
        )

    async def _recorded_render(self, target: AutomaticTarget, row) -> ObjectRef:
        """The render `row` records (its version, under its camera). Missing
        from Garage, the step fails: a row names an image nobody can read,
        and deferring would hold its mission in the cohort for good."""
        ref = await self._store.locate_processed_jpeg(
            target.tenant_id,
            automatic_jpeg_folder(
                row.jpeg_render_version, row.jpeg_camera_calibration_id
            ),
            row.checksum,
            from_v1=False,
        )
        if ref is None:
            raise ApplicationError(
                f"dive {target.mission_id}: capture {row.capture_id}'s render "
                f"r{row.jpeg_render_version} under camera "
                f"{row.jpeg_camera_calibration_id} is not in Garage",
                type="AutomaticRenderMissing",
                non_retryable=True,
            )
        return ref

    async def _write(self, target: AutomaticTarget, fn, *args, **kwargs):
        try:
            return await fn(target.tenant_id, target.mission_id, *args, **kwargs)
        except InvalidAutomaticResults as exc:
            raise _refuse(target, str(exc)) from exc

    # -- select ---------------------------------------------------------------

    @activity.defn(name="select_next_dive_for_automatic_results")
    async def select_next_mission_for_automatic_results(
        self, exclude: Optional[List[uuid.UUID]] = None
    ) -> Optional[AutomaticTarget]:
        """The oldest backlog dive across tenants, other than those the run
        already took."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_mission_for_automatic_results(
                tenant_id,
                species=self._species_enabled,
                exclude=exclude or [],
                **VERSIONS,
            )
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.mission_id))
            if best_key is None or key < best_key:
                best, best_key = AutomaticTarget(tenant_id, candidate.mission_id), key
        activity.logger.info("next dive for automatic results: %s", best)
        return best

    @activity.defn(name="record_automatic_results_failure")
    async def record_automatic_results_failure(
        self, target: AutomaticTarget, step: str, error: str
    ) -> None:
        """A dive's failed step; at three failures the cohort leaves the dive
        for an operator (0040)."""
        await self._catalog.record_automatic_results_failure(
            target.tenant_id, target.mission_id, step=step, error=error
        )

    @activity.defn(name="plan_automatic_run")
    async def plan_automatic_run(self, tenant: str, numbers: List[int]) -> dict:
        """A by-hand run's plan (`run_missions`): the named dives, sessions
        first, as JSON for the operator's reviewed report."""
        tenant_id = await self._catalog.tenant_by_slug(tenant)
        if tenant_id is None:
            raise ApplicationError(
                f"the orchestrator is no member of a tenant {tenant!r}",
                type="NotAMember",
                non_retryable=True,
            )
        plan = await self._catalog.automatic_run_plan(
            tenant_id,
            list(numbers),
            headtail_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )
        # On the wire under the names a running or rolled-back workflow reads
        # (Phase 2a); the workflow reads either (`run_missions.with_current_names`).
        return {
            "tenant_id": str(tenant_id),
            "dives": [
                {
                    **{
                        k: v
                        for k, v in dataclasses.asdict(d).items()
                        if k != "mission_id"
                    },
                    "dive_id": str(d.mission_id),
                }
                for d in plan.missions
            ],
            "missing": list(plan.missing),
        }

    @activity.defn(name="record_automatic_frame_failures")
    async def record_automatic_frame_failures(
        self, target: AutomaticTarget, captures: List[uuid.UUID], error: str
    ) -> List[uuid.UUID]:
        """Frames the processor left out of a run, recorded against each
        frame -- never one of the dive's strikes (0044). Returns the frames
        the store has given up on (their terminal ``frame_failed`` rows)."""
        return await self._write(
            target,
            self._catalog.record_automatic_frame_failures,
            [(c, error) for c in captures],
            headtail_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )

    # -- frames (GPU) ---------------------------------------------------------

    @activity.defn(name="resolve_automatic_frames_inputs")
    async def resolve_automatic_frames_inputs(
        self, target: AutomaticTarget
    ) -> PredictAutomaticFramesInput:
        inputs = await self._catalog.automatic_frames_inputs(
            target.tenant_id,
            target.mission_id,
            headtail_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )
        frames = []
        for f in inputs.frames:
            frames.append(
                AutomaticFrame(
                    capture_id=f.capture_id,
                    raw=self._store.layout.raw(target.tenant_id, f.checksum),
                    jpeg=self._automatic_jpeg_key(
                        target, f.checksum, inputs.camera_calibration_id
                    ),
                    write_jpeg=True,
                    jpeg_render_version=AUTOMATIC_JPEG_RENDER_VERSION,
                    slate_probability=f.slate_probability,
                    is_slate=f.is_slate,
                )
            )
            activity.heartbeat()
        activity.logger.info(
            "automatic frames dive=%s frames=%d slate=%d",
            target.mission_id, len(frames), sum(f.is_slate for f in frames),
        )  # fmt: skip
        return PredictAutomaticFramesInput(
            tenant_id=target.tenant_id,
            mission_id=target.mission_id,
            camera_matrix=inputs.camera_matrix,
            distortion_coefficients=inputs.distortion_coefficients,
            camera_calibration_id=inputs.camera_calibration_id,
            laser_region=[list(v) for v in LASER_REGION_POLYGON],
            frames=frames,
        )

    @activity.defn(name="persist_automatic_frames")
    async def persist_automatic_frames(
        self, target: AutomaticTarget, results: List[AutomaticFrameResult]
    ) -> int:
        if refused := sorted(
            {r.status for r in results} - set(AUTOMATIC_HEADTAIL_STATUSES)
        ):
            raise _refuse(target, f"status {refused} is not an automatic row")
        if any(
            r.predictor_version != AUTOMATIC_HEADTAIL_PREDICTOR_VERSION for r in results
        ):
            raise _refuse(target, "a result from another automatic version")
        # A row's pixels belong to the render it was found in: every frame
        # whose raw decoded was rendered, and must name the render this
        # orchestrator asks for. One naming none was resolved before the
        # deploy (contract 7): its pixels are in no automatic render, so it is
        # not written and stays owed -- not a failure of the dive (review of
        # #136). One naming another is a processor at another version.
        if any(
            r.jpeg_render_version is not None
            and r.jpeg_render_version != AUTOMATIC_JPEG_RENDER_VERSION
            for r in results
        ):
            raise _refuse(target, "a frame found in another render")
        unnamed = [
            r
            for r in results
            if r.status not in _NO_RENDER and r.jpeg_render_version is None
        ]
        if unnamed:
            activity.logger.warning(
                "dive=%s: %d frames came back with no render named (resolved "
                "before the render was versioned); left owed",
                target.mission_id,
                len(unnamed),
            )
        rows = [
            AutomaticHeadTailRow(**r.model_dump())
            for r in results
            if r.status in _NO_RENDER or r.jpeg_render_version is not None
        ]
        written = await self._write(
            target, self._catalog.persist_automatic_head_tails, rows
        )
        activity.logger.info(
            "dive=%s automatic frames written=%d", target.mission_id, written
        )
        return written

    # -- species (GPU; the species stage's processor workflow) ------------------

    @activity.defn(name="resolve_automatic_species_inputs")
    async def resolve_automatic_species_inputs(
        self, target: AutomaticTarget
    ) -> PredictSpeciesImagesInput:
        if not self._species_enabled:
            return PredictSpeciesImagesInput(
                tenant_id=target.tenant_id,
                mission_id=target.mission_id,
                candidates=species_candidates(),
                images=[],
            )
        captures = await self._catalog.automatic_species_captures(
            target.tenant_id,
            target.mission_id,
            species_version=SPECIES_PREDICTOR_VERSION,
        )
        images = []
        for c in captures:
            # Every capture here has a render recorded (the store selects them).
            jpeg = await self._recorded_render(target, c)
            images.append(
                PredictSpeciesImage(
                    capture_id=c.capture_id,
                    headtail_prediction_id=c.automatic_head_tail_prediction_id,
                    jpeg=jpeg,
                    mask_bbox=c.mask_bbox,
                    has_existing_prediction=c.has_existing_prediction,
                )
            )
        return PredictSpeciesImagesInput(
            tenant_id=target.tenant_id,
            mission_id=target.mission_id,
            candidates=species_candidates(),
            images=images,
        )

    @activity.defn(name="persist_automatic_species")
    async def persist_automatic_species(
        self, target: AutomaticTarget, results: List[SpeciesPredictionResult]
    ) -> int:
        if refused := sorted({r.status for r in results} - set(SPECIES_STATUSES)):
            raise _refuse(target, f"species status {refused} is not a prediction")
        choices = {c.choice for c in species_candidates()}
        named = {r.predicted_choice for r in results if r.predicted_choice} | {
            s.choice for r in results for s in r.top5
        }
        if foreign := sorted(named - choices):
            raise _refuse(target, f"{foreign} are not candidates")
        if any(r.predictor_version is None or not r.model_id for r in results):
            raise _refuse(target, "a species result names no version or model")
        rows = [
            AutomaticSpeciesRow(
                capture_id=r.capture_id,
                automatic_head_tail_prediction_id=r.headtail_prediction_id,
                status=r.status,
                predictor_version=r.predictor_version,
                model_id=r.model_id,
                predicted_choice=r.predicted_choice,
                top1_probability=r.top1_probability,
                margin=r.margin,
                top5=[s.model_dump() for s in r.top5],
            )
            for r in results
        ]
        return await self._write(target, self._catalog.persist_automatic_species, rows)

    # -- calibration (per-image) -------------------------------------------------

    @activity.defn(name="resolve_automatic_calibration_inputs")
    async def resolve_automatic_calibration_inputs(
        self, target: AutomaticTarget
    ) -> AutomaticCalibrationPlan:
        inputs = await self._catalog.automatic_calibration_inputs(
            target.tenant_id, target.mission_id
        )
        frames = []
        unrendered = [c for c in inputs.candidates if c.jpeg_render_version is None]
        other_camera = [
            c
            for c in inputs.candidates
            if c.jpeg_render_version is not None
            and c.jpeg_camera_calibration_id != inputs.camera_calibration_id
        ]
        if unrendered or other_camera:
            # The fit reads features through the mission's current camera: a
            # frame rendered under another, or never rendered, is not in it.
            activity.logger.warning(
                "dive=%s: %d of %d slate frames left out of the fit: "
                "%d with no render, %d rendered under another camera",
                target.mission_id,
                len(unrendered) + len(other_camera),
                len(inputs.candidates),
                len(unrendered),
                len(other_camera),
            )
        skip = {c.capture_id for c in (*unrendered, *other_camera)}
        for c in inputs.candidates:
            if c.capture_id in skip:
                continue
            jpeg = await self._recorded_render(target, c)
            frames.append(
                AutomaticCalibrationFrame(capture_id=c.capture_id, jpeg=jpeg,
                                          x=c.x, y=c.y)  # fmt: skip
            )
        return AutomaticCalibrationPlan(
            payload=FitAutomaticCalibrationInput(
                tenant_id=target.tenant_id,
                mission_id=target.mission_id,
                camera_matrix=inputs.camera_matrix,
                frames=frames,
                line_dots=inputs.line_dots,
            ),
            camera_calibration_id=inputs.camera_calibration_id,
        )

    @activity.defn(name="persist_automatic_calibration")
    async def persist_automatic_calibration(
        self,
        target: AutomaticTarget,
        plan: AutomaticCalibrationPlan,
        result: AutomaticCalibrationResult,
    ) -> uuid.UUID:
        if result.mission_id != target.mission_id:
            raise _refuse(target, f"a calibration for dive {result.mission_id}")
        fields = result.model_dump(exclude={"mission_id"})
        row = AutomaticCalibrationRow(
            camera_calibration_id=plan.camera_calibration_id, **fields
        )
        return await self._write(
            target, self._catalog.persist_automatic_calibration, row
        )

    # -- lengths (light) -----------------------------------------------------------

    @activity.defn(name="resolve_automatic_measure_inputs")
    async def resolve_automatic_measure_inputs(
        self, target: AutomaticTarget
    ) -> Optional[AutomaticMeasurePlan]:
        inputs = await self._catalog.automatic_measure_inputs(
            target.tenant_id,
            target.mission_id,
            algorithm_version=AUTOMATIC_MEASUREMENT_VERSION,
        )
        if inputs.calibration is None:
            return None
        cal = inputs.calibration
        return AutomaticMeasurePlan(
            payload=MeasureAutomaticInput(
                tenant_id=target.tenant_id,
                mission_id=target.mission_id,
                camera_matrix=inputs.camera_matrix,
                laser_position=cal.laser_position,
                laser_axis=cal.laser_axis,
                captures=[AutomaticMeasureCapture(**vars(c)) for c in inputs.captures],
            ),
            calibration_source=cal.source,
            automatic_laser_calibration_id=cal.automatic_laser_calibration_id,
            laser_calibration_id=cal.laser_calibration_id,
            camera_calibration_id=inputs.camera_calibration_id,
        )

    @activity.defn(name="persist_automatic_measurements")
    async def persist_automatic_measurements(
        self,
        target: AutomaticTarget,
        plan: AutomaticMeasurePlan,
        result: MeasureAutomaticResult,
    ) -> int:
        if result.mission_id != target.mission_id:
            raise _refuse(target, f"lengths for dive {result.mission_id}")
        rows = [
            AutomaticMeasurementRow(
                capture_id=x.capture_id,
                automatic_head_tail_prediction_id=x.automatic_head_tail_prediction_id,
                calibration_source=plan.calibration_source,
                automatic_laser_calibration_id=plan.automatic_laser_calibration_id,
                laser_calibration_id=plan.laser_calibration_id,
                camera_calibration_id=plan.camera_calibration_id,
                length_m=x.length_m,
                depth_m=x.depth_m,
                refusal=x.refusal,
                algorithm=result.algorithm,
                algorithm_version=result.algorithm_version,
                core_version=result.core_version,
            )
            for x in result.lengths
        ]
        return await self._write(
            target, self._catalog.persist_automatic_measurements, rows
        )
