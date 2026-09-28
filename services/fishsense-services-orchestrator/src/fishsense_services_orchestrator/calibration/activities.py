"""The calibration stages' orchestrator activities: select, resolve, record.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(select_next_high_priority_dive_for_laser_calibration_activity.py,
select_next_high_priority_dive_for_checkerboard_calibration_activity.py,
resolve_checkerboard_calibration_inputs_activity.py), the SDK reads and writes
v1's data-worker made in perform_laser_calibration_activity.py and
fit_checkerboard_laser_extrinsics.py (`get_*`, `put_laser_extrinsics`,
`set_calibration_refused`), and the lattice study's resolver reuse.

v2 changes:

* selectors take the oldest candidate across every tenant served, with
  re-entry candidates (a stored fit whose baseline is implausible) after
  every fresh one -- v1's `_reentry_last`, across tenants;
* **stage 13's inputs are resolved here**, not by the processor: the slate
  template, each live slate label with its frame's lowest live laser dot,
  the camera calibration and the dive's dots;
* every resolver returns the provenance the result is recorded with, and
  the result is appended by `record_laser_calibration` (v1 upserted the
  extrinsics, or set the dive's refusal columns, from the data-worker);
* a dive that cannot be resolved is a final refusal;
* the lattice study resolves its tenant by slug and each dive by number.
"""

from __future__ import annotations

import uuid
from typing import Optional, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.laser_calibration_store import (
    CalibrationCandidate,
    CalibrationInputsUnavailable,
    CalibrationRecord,
    CheckerboardInputs,
    SlateCalibrationInputs,
)
from fishsense_services_contracts.object_store import (
    CHECKERBOARD_LATTICE_JPEG_FOLDER,
)
from fishsense_services_contracts.slate_calibration import (
    CheckerboardCalibrationImage,
    CheckerboardTarget,
    LatticeImage,
    PerformCheckerboardCalibrationInput,
    SlateCalibrationInput,
    SlateObservation,
    VerifyCheckerboardLatticeInput,
)
from fishsense_services_orchestrator.calibration.contracts import (
    CalibrationProvenance,
    CheckerboardCalibrationPlan,
    LatticeDive,
    LatticePlan,
    RecordCalibration,
    SlateCalibrationPlan,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

__all__ = ["LaserCalibrationActivities", "LaserCalibrationCatalog"]


class LaserCalibrationCatalog(Protocol):
    """What the calibration stages ask the database; see
    ``fishsense_services_api.laser_calibration_store.LaserCalibrationCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def resolve_tenant(self, slug: str) -> uuid.UUID | None: ...

    async def next_dive_for_laser_calibration(
        self, tenant_id: uuid.UUID
    ) -> CalibrationCandidate | None: ...

    async def next_dive_for_checkerboard_calibration(
        self, tenant_id: uuid.UUID
    ) -> CalibrationCandidate | None: ...

    async def slate_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SlateCalibrationInputs | None: ...

    async def checkerboard_calibration_inputs(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> CheckerboardInputs: ...

    async def dive_for_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None: ...

    async def record_laser_calibration(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, record: CalibrationRecord
    ) -> uuid.UUID: ...


def _unavailable(target_dive, exc: CalibrationInputsUnavailable) -> ApplicationError:
    return ApplicationError(
        f"cannot resolve a calibration for dive {target_dive}: {exc}",
        type="CalibrationInputsUnavailable",
        non_retryable=True,
    )


class LaserCalibrationActivities:
    def __init__(
        self, *, catalog: LaserCalibrationCatalog, store: OrchestratorObjectStore
    ) -> None:
        self._catalog = catalog
        self._store = store

    async def _oldest(self, next_for, what: str) -> Optional[StagingTarget]:
        best: StagingTarget | None = None
        best_key = None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await next_for(tenant_id)
            if candidate is None:
                continue
            key = (candidate.reentry, candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = StagingTarget(tenant_id, candidate.dive_id), key
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

    @activity.defn(name="select_next_dive_for_laser_calibration")
    async def select_next_dive_for_laser_calibration(self) -> Optional[StagingTarget]:
        """The next dive stage 13 can calibrate from its slate, across tenants."""
        return await self._oldest(
            self._catalog.next_dive_for_laser_calibration, "laser calibration"
        )

    @activity.defn(name="select_next_dive_for_checkerboard_calibration")
    async def select_next_dive_for_checkerboard_calibration(
        self,
    ) -> Optional[StagingTarget]:
        """The next dive to calibrate from its checkerboard, across tenants."""
        return await self._oldest(
            self._catalog.next_dive_for_checkerboard_calibration,
            "checkerboard laser calibration",
        )

    @activity.defn(name="resolve_slate_calibration_inputs")
    async def resolve_slate_calibration_inputs(
        self, target: StagingTarget
    ) -> Optional[SlateCalibrationPlan]:
        """Stage 13's payload and provenance; None when there is nothing to
        fit (no slate template, or no slate labels: v1's no-op)."""
        try:
            inputs = await self._catalog.slate_calibration_inputs(
                target.tenant_id, target.dive_id
            )
        except CalibrationInputsUnavailable as exc:
            raise _unavailable(target.dive_id, exc) from exc
        if inputs is None:
            return None
        activity.logger.info(
            "resolved stage 13 inputs dive=%s observations=%d dive_dots=%d",
            target.dive_id,
            len(inputs.observations),
            len(inputs.dive_dots),
        )
        return SlateCalibrationPlan(
            payload=SlateCalibrationInput(
                dive_id=target.dive_id,
                camera_matrix=inputs.camera_matrix,
                template_points=inputs.template_points,
                dpi=inputs.dpi,
                observations=[
                    SlateObservation(
                        capture_id=o.capture_id,
                        reference_points=o.reference_points,
                        skipped_points=o.skipped_points,
                        laser_x=o.laser_x,
                        laser_y=o.laser_y,
                    )
                    for o in inputs.observations
                ],
                dive_dots=inputs.dive_dots,
            ),
            provenance=CalibrationProvenance(
                producer="slate",
                camera_calibration_id=inputs.camera_calibration_id,
                slate_template_id=inputs.slate_template_id,
                inputs_as_of=inputs.inputs_as_of,
            ),
        )

    async def _board(self, tenant_id, dive_id) -> CheckerboardInputs:
        try:
            return await self._catalog.checkerboard_calibration_inputs(
                tenant_id, dive_id
            )
        except CalibrationInputsUnavailable as exc:
            raise _unavailable(dive_id, exc) from exc

    @staticmethod
    def _target(inputs: CheckerboardInputs) -> CheckerboardTarget:
        return CheckerboardTarget(
            rows=inputs.rows,
            cols=inputs.cols,
            pitch_x_m=inputs.pitch_x_m,
            pitch_y_m=inputs.pitch_y_m,
        )

    @activity.defn(name="resolve_checkerboard_calibration_inputs")
    async def resolve_checkerboard_calibration_inputs(
        self, target: StagingTarget
    ) -> CheckerboardCalibrationPlan:
        """The camera, the board's current geometry, and one frame per
        canonical image with a live dot -- "frames worth trying", never
        "frames that will work": the board is found by the processor."""
        inputs = await self._board(target.tenant_id, target.dive_id)
        layout = self._store.layout
        activity.logger.info(
            "resolved checkerboard calibration inputs dive=%s board=%dx%d "
            "pitch=%.5f/%.5fm frames=%d",
            target.dive_id,
            inputs.rows,
            inputs.cols,
            inputs.pitch_x_m,
            inputs.pitch_y_m,
            len(inputs.frames),
        )
        return CheckerboardCalibrationPlan(
            payload=PerformCheckerboardCalibrationInput(
                dive_id=target.dive_id,
                camera_matrix=inputs.camera_matrix,
                distortion_coefficients=inputs.distortion_coefficients,
                target=self._target(inputs),
                images=[
                    CheckerboardCalibrationImage(
                        capture_id=frame.capture_id,
                        raw=layout.raw(target.tenant_id, frame.checksum),
                        laser_x=frame.laser_x,
                        laser_y=frame.laser_y,
                    )
                    for frame in inputs.frames
                ],
                dive_dots=inputs.dive_dots,
            ),
            provenance=CalibrationProvenance(
                producer="checkerboard",
                camera_calibration_id=inputs.camera_calibration_id,
                calibration_target_id=inputs.calibration_target_id,
                inputs_as_of=inputs.inputs_as_of,
            ),
        )

    @activity.defn(name="record_laser_calibration")
    async def record_laser_calibration(self, payload: RecordCalibration) -> str:
        """Append one attempt, accepted or refused, with its provenance."""
        result, provenance = payload.result, payload.provenance
        verdicts: dict = {
            **result.gate_verdicts,
            "observations_trimmed": result.observations_trimmed,
        }
        if result.refusal_type:
            verdicts["refusal_type"] = result.refusal_type
        written = await self._catalog.record_laser_calibration(
            payload.tenant_id,
            payload.dive_id,
            CalibrationRecord(
                producer=provenance.producer,
                outcome=result.outcome,
                laser_position=result.laser_position,
                laser_axis=result.laser_axis,
                refusal_reason=result.refusal_reason,
                gate_verdicts=verdicts,
                lever_arm_m=result.lever_arm_m,
                observation_count=result.observation_count,
                core_version=result.core_version,
                camera_calibration_id=provenance.camera_calibration_id,
                slate_template_id=provenance.slate_template_id,
                calibration_target_id=provenance.calibration_target_id,
                inputs_as_of=provenance.inputs_as_of,
            ),
        )
        activity.logger.info(
            "recorded laser calibration dive=%s producer=%s outcome=%s id=%s",
            payload.dive_id,
            provenance.producer,
            result.outcome,
            written,
        )
        return str(written)

    # --- the lattice study ----------------------------------------------------

    @activity.defn(name="resolve_lattice_tenant")
    async def resolve_lattice_tenant(self, slug: str) -> uuid.UUID:
        """The study's tenant, if this orchestrator serves it."""
        tenant_id = await self._catalog.resolve_tenant(slug)
        if tenant_id is None:
            raise ApplicationError(
                f"tenant {slug!r} is not one this orchestrator serves",
                type="NotAMember",
                non_retryable=True,
            )
        return tenant_id

    @activity.defn(name="resolve_lattice_inputs")
    async def resolve_lattice_inputs(self, dive: LatticeDive) -> LatticePlan:
        """The frames the dive's checkerboard fit consumed -- the calibration
        resolver's population, reused so the study cannot drift from it --
        with the key each render is written to."""
        dive_id = await self._catalog.dive_for_number(dive.tenant_id, dive.number)
        if dive_id is None:
            raise ApplicationError(
                f"tenant {dive.tenant_id} has no dive numbered {dive.number}",
                type="UnknownDive",
                non_retryable=True,
            )
        inputs = await self._board(dive.tenant_id, dive_id)
        layout = self._store.layout
        return LatticePlan(
            target=StagingTarget(tenant_id=dive.tenant_id, dive_id=dive_id),
            payload=VerifyCheckerboardLatticeInput(
                dive_id=dive_id,
                camera_matrix=inputs.camera_matrix,
                distortion_coefficients=inputs.distortion_coefficients,
                target=self._target(inputs),
                images=[
                    LatticeImage(
                        capture_id=frame.capture_id,
                        raw=layout.raw(dive.tenant_id, frame.checksum),
                        render=layout.processed_jpeg(
                            dive.tenant_id,
                            CHECKERBOARD_LATTICE_JPEG_FOLDER,
                            frame.checksum,
                        ),
                        laser_x=frame.laser_x,
                        laser_y=frame.laser_y,
                    )
                    for frame in inputs.frames
                ],
                sample_limit=dive.sample_limit,
            ),
        )
