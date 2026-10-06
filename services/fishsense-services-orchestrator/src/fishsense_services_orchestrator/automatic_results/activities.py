"""The automatic-results track's orchestrator activities.

New in v2: fish lengths with no human label (cscw-fishsense2027@96a8da07
PAPER.md §6), as their own track (decided 2026-10-06). Built as head/tail
prediction's and species prediction's activities are, and keeping their
rules:

* the target is (tenant, dive); the selector takes the oldest backlog dive
  across every tenant the orchestrator serves;
* **the orchestrator issues the keys** (PLAN.md §9.11): a frame's staged raw,
  and the head/tail stage's JPEG key -- where the JPEG already is (read, left
  alone), else the tenant's (written by the GPU stage);
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

import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol

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
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER
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


@dataclass(frozen=True)
class AutomaticTarget:
    """A dive of a tenant."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID


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

    async def next_dive_for_automatic_results(
        self, tenant_id: uuid.UUID, **versions
    ) -> AutomaticCandidate | None: ...

    async def automatic_frames_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, headtail_version: int
    ) -> AutomaticFramesInputs: ...

    async def persist_automatic_head_tails(self, tenant_id, dive_id, rows) -> int: ...

    async def automatic_species_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, species_version: int
    ) -> list[AutomaticSpeciesCapture]: ...

    async def persist_automatic_species(self, tenant_id, dive_id, rows) -> int: ...

    async def automatic_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> AutomaticCalibrationInputs: ...

    async def persist_automatic_calibration(
        self, tenant_id, dive_id, row: AutomaticCalibrationRow
    ) -> uuid.UUID: ...

    async def automatic_measure_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, algorithm_version: str
    ) -> AutomaticMeasureInputs: ...

    async def persist_automatic_measurements(self, tenant_id, dive_id, rows) -> int: ...


def _refuse(target: AutomaticTarget, why: str) -> ApplicationError:
    return ApplicationError(
        f"refusing the processor's automatic results for dive {target.dive_id}: {why}",
        type="InvalidPredictions",
        non_retryable=True,
    )


class AutomaticResultsActivities:
    def __init__(self, *, catalog: AutomaticResultsCatalog, store: Any) -> None:
        self._catalog = catalog
        self._store = store

    async def _jpeg(self, target: AutomaticTarget, checksum: str, from_v1: bool):
        return await self._store.locate_processed_jpeg(
            target.tenant_id, HEADTAIL_JPEG_FOLDER, checksum, from_v1=from_v1
        )

    async def _write(self, target: AutomaticTarget, fn, *args):
        try:
            return await fn(target.tenant_id, target.dive_id, *args)
        except InvalidAutomaticResults as exc:
            raise _refuse(target, str(exc)) from exc

    # -- select ---------------------------------------------------------------

    @activity.defn(name="select_next_dive_for_automatic_results")
    async def select_next_dive_for_automatic_results(
        self,
    ) -> Optional[AutomaticTarget]:
        """The oldest backlog dive across tenants."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_automatic_results(
                tenant_id, **VERSIONS
            )
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = AutomaticTarget(tenant_id, candidate.dive_id), key
        activity.logger.info("next dive for automatic results: %s", best)
        return best

    # -- frames (GPU) ---------------------------------------------------------

    @activity.defn(name="resolve_automatic_frames_inputs")
    async def resolve_automatic_frames_inputs(
        self, target: AutomaticTarget
    ) -> PredictAutomaticFramesInput:
        inputs = await self._catalog.automatic_frames_inputs(
            target.tenant_id,
            target.dive_id,
            headtail_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )
        frames = []
        for f in inputs.frames:
            located = await self._jpeg(target, f.checksum, f.from_v1)
            frames.append(
                AutomaticFrame(
                    capture_id=f.capture_id,
                    raw=self._store.layout.raw(target.tenant_id, f.checksum),
                    jpeg=located
                    or self._store.layout.processed_jpeg(
                        target.tenant_id, HEADTAIL_JPEG_FOLDER, f.checksum
                    ),
                    write_jpeg=located is None,
                    slate_probability=f.slate_probability,
                    is_slate=f.is_slate,
                )
            )
            activity.heartbeat()
        activity.logger.info(
            "automatic frames dive=%s frames=%d slate=%d",
            target.dive_id, len(frames), sum(f.is_slate for f in frames),
        )  # fmt: skip
        return PredictAutomaticFramesInput(
            tenant_id=target.tenant_id,
            dive_id=target.dive_id,
            camera_matrix=inputs.camera_matrix,
            distortion_coefficients=inputs.distortion_coefficients,
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
        rows = [AutomaticHeadTailRow(**r.model_dump()) for r in results]
        written = await self._write(
            target, self._catalog.persist_automatic_head_tails, rows
        )
        activity.logger.info(
            "dive=%s automatic frames written=%d", target.dive_id, written
        )
        return written

    # -- species (GPU; the species stage's processor workflow) ------------------

    @activity.defn(name="resolve_automatic_species_inputs")
    async def resolve_automatic_species_inputs(
        self, target: AutomaticTarget
    ) -> PredictSpeciesImagesInput:
        captures = await self._catalog.automatic_species_captures(
            target.tenant_id, target.dive_id, species_version=SPECIES_PREDICTOR_VERSION
        )
        images = []
        for c in captures:
            jpeg = await self._jpeg(target, c.checksum, c.from_v1)
            if jpeg is None:
                activity.logger.info(
                    "no JPEG yet for capture %s; deferring", c.capture_id
                )
                continue
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
            dive_id=target.dive_id,
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
            target.tenant_id, target.dive_id
        )
        frames = []
        for c in inputs.candidates:
            jpeg = await self._jpeg(target, c.checksum, c.from_v1)
            if jpeg is not None:
                frames.append(
                    AutomaticCalibrationFrame(capture_id=c.capture_id, jpeg=jpeg,
                                              x=c.x, y=c.y)  # fmt: skip
                )
        return AutomaticCalibrationPlan(
            payload=FitAutomaticCalibrationInput(
                tenant_id=target.tenant_id,
                dive_id=target.dive_id,
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
        if result.dive_id != target.dive_id:
            raise _refuse(target, f"a calibration for dive {result.dive_id}")
        fields = result.model_dump(exclude={"dive_id"})
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
            target.dive_id,
            algorithm_version=AUTOMATIC_MEASUREMENT_VERSION,
        )
        if inputs.calibration is None:
            return None
        cal = inputs.calibration
        return AutomaticMeasurePlan(
            payload=MeasureAutomaticInput(
                tenant_id=target.tenant_id,
                dive_id=target.dive_id,
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
        if result.dive_id != target.dive_id:
            raise _refuse(target, f"lengths for dive {result.dive_id}")
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
