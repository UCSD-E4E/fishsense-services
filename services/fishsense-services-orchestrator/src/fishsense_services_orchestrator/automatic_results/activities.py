"""The automatic-results track's orchestrator activities.

New in v2: fish lengths with no human label (cscw-fishsense2027@96a8da07
PAPER.md §6), as their own track (decided 2026-10-06). Built as head/tail
prediction's and species prediction's activities are, and keeping their
rules:

* the target is (tenant, dive); the selector takes the oldest backlog dive
  across every tenant the orchestrator serves;
* **the orchestrator issues the keys** (PLAN.md §9.11): a frame's staged raw,
  and a presigned GET of its render in the tenant's own bucket;
* **one image per frame, rectified from the raw** (2026-10-19): the render
  stage (`renders`) rectifies every capture's raw with its camera's matrix and
  distortion -- the dot's pixel space -- into the tenant's bucket. SAM finds
  the mask in it, species crops it, and the calibration takes its features
  from it, each by presigned GET of the render its row records (version and
  camera). A frame with no render at the current version under the mission's
  camera gets none (the workflow renders it first); a row naming a render
  nobody recorded fails its step. These steps used to read the head/tail
  JPEG, which for a migrated frame is v1's render, the image human labels were
  drawn on: this track never writes it;
* species reuse the species stage's processor contract unchanged, cropped by
  the automatic mask's box, with the automatic head/tail's id in the
  `headtail_prediction_id` slot (it is echoed back, and the write checks it is
  the capture's own);
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
from fishsense_services_contracts.renders import render_key
from fishsense_services_contracts.species_prediction import (
    SPECIES_PREDICTOR_VERSION,
    SPECIES_STATUSES,
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesPredictionResult,
)
from fishsense_services_contracts.masks import MaskUpload
from fishsense_services_orchestrator.masks import checked_masks, discard, mask_upload
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


#: A loop over a mission's frames heartbeats once per this many, not per
#: frame: the worker queues heartbeats (1,000 at most), and a loop with nothing
#: to await between them fills the queue -- mission 440's 1,122 frames failed
#: with asyncio's QueueFull (2026-10-20).
HEARTBEAT_EVERY = 250

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
        renders: Any,
        species_enabled: bool = True,
    ) -> None:
        self._catalog = catalog
        self._store = store
        #: The captures' renders, read by presigned GET (`renders.urls`).
        self._renders = renders
        #: Species' own switch (2026-10-15): off, no species images, and the
        #: cohort holds no dive for species work.
        self._species_enabled = species_enabled

    async def _recorded_renders(self, target: AutomaticTarget, rows) -> list[str]:
        """Presigned GETs of the render each of `rows` records -- its version,
        under its camera. One not recorded there fails the step: its row names
        an image nobody can read, and deferring would hold its mission in the
        cohort for good (review of #144: the newest render under any camera
        presigned a key that did not exist)."""
        by_render: dict[tuple, list] = {}
        for row in rows:
            render = (row.jpeg_render_version, row.jpeg_camera_calibration_id)
            by_render.setdefault(render, []).append(row)
        recorded = {}
        for (version, camera), group in by_render.items():
            recorded.update(
                await self._renders.at(
                    target.tenant_id,
                    [row.capture_id for row in group],
                    render_version=version,
                    camera_calibration_id=camera,
                )
            )
        presign = await self._renders.presigner(target.tenant_id) if rows else None
        urls = []
        for row in rows:
            render = recorded.get(row.capture_id)
            if render is None or render.status != "rendered":
                raise ApplicationError(
                    f"dive {target.mission_id}: capture {row.capture_id} has no "
                    f"render recorded at r{row.jpeg_render_version} under camera "
                    f"{row.jpeg_camera_calibration_id}",
                    type="AutomaticRenderMissing",
                    non_retryable=True,
                )
            urls.append(presign(render.key))
        return urls

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
        renders = (
            await self._renders.at(
                target.tenant_id,
                [f.capture_id for f in inputs.frames],
                render_version=AUTOMATIC_JPEG_RENDER_VERSION,
                camera_calibration_id=inputs.camera_calibration_id,
            )
            if inputs.frames
            else {}
        )
        presign = await self._renders.presigner(target.tenant_id) if renders else None
        # A frame whose render settled as an abstention has none and never
        # will: it is that abstention, recorded here, so it stops holding its
        # mission (review of #144).
        settled = [
            AutomaticHeadTailRow(
                capture_id=f.capture_id,
                status=renders[f.capture_id].status,
                predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
                slate_probability=f.slate_probability,
            )
            for f in inputs.frames
            if f.capture_id in renders
            and renders[f.capture_id].status != "rendered"
            and renders[f.capture_id].settled
        ]
        if settled:
            await self._write(
                target, self._catalog.persist_automatic_head_tails, settled
            )
            activity.logger.info(
                "dive=%s: %d frames have no render and never will (abstentions)",
                target.mission_id,
                len(settled),
            )
        done = {r.capture_id for r in settled}
        for f in inputs.frames:
            if f.capture_id in done:
                continue
            render = renders.get(f.capture_id)
            rendered = render is not None and render.status == "rendered"
            frames.append(
                AutomaticFrame(
                    capture_id=f.capture_id,
                    raw=self._store.layout.raw(target.tenant_id, f.checksum),
                    jpeg_render_version=AUTOMATIC_JPEG_RENDER_VERSION,
                    render_url=presign(render.key) if rendered else None,
                    slate_probability=f.slate_probability,
                    is_slate=f.is_slate,
                )
            )
            if len(frames) % HEARTBEAT_EVERY == 0:
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

    @activity.defn(name="presign_automatic_masks")
    async def presign_automatic_masks(
        self, target: AutomaticTarget, capture_ids: List[uuid.UUID]
    ) -> List[Optional[MaskUpload]]:
        """A mask upload for each of a GPU chunk's frames (contract 12): a new
        key in the tenant's bucket and its presigned PUT; none for a tenant
        with no bucket. Per chunk, not at resolve: a URL per frame of a whole
        large mission would pass Temporal's 2 MB payload limit."""
        objects = await self._renders.objects(target.tenant_id)
        if objects is None:
            return [None] * len(capture_ids)
        return [mask_upload(objects, "automatic", c) for c in capture_ids]

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
        kept = [
            r
            for r in results
            if r.status in _NO_RENDER or r.jpeg_render_version is not None
        ]
        rows = [AutomaticHeadTailRow(**r.model_dump(exclude={"mask"})) for r in kept]
        # Contract 12: each stored mask, read back and checked, is named
        # against its row's prediction in the same transaction; one that fails,
        # or whose frame is not written, is deleted rather than left unnamed.
        masks: list = []
        if any(r.mask is not None for r in results):
            objects = await self._renders.objects(target.tenant_id)
            if objects is not None:
                masks, failed = await checked_masks(objects, "automatic", kept)
                written_ids = {id(r) for r in kept}
                dropped = [r.mask.key for r in results
                           if r.mask is not None and id(r) not in written_ids]  # fmt: skip
                await discard(objects, failed + dropped)
        written = await self._write(
            target,
            self._catalog.persist_automatic_head_tails,
            rows,
            masks if any(m is not None for m in masks) else [],
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
        # Every capture here has a render named (the store selects them).
        urls = await self._recorded_renders(target, captures)
        for c, url in zip(captures, urls):
            images.append(
                PredictSpeciesImage(
                    capture_id=c.capture_id,
                    headtail_prediction_id=c.automatic_head_tail_prediction_id,
                    jpeg_url=url,
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
        kept = [c for c in inputs.candidates if c.capture_id not in skip]
        for c, url in zip(kept, await self._recorded_renders(target, kept)):
            frames.append(
                AutomaticCalibrationFrame(capture_id=c.capture_id, jpeg_url=url,
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
