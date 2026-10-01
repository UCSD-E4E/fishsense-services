"""Stage 14's orchestrator activities: select, resolve, persist.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
select_next_high_priority_dive_for_measure_fish_activity.py, plus the read and
write halves of the data-worker's measure_fish_activity.py (the dive's
calibration context, species labels, clusters and measurements it read; the
species, fish, cluster bindings and measurements it wrote). v2's call the
measurement catalog (`fishsense_services_api.measurement_store`), which owns
the cohort, the work, the fish binding and the persistence rules; the
processor only computes lengths.

v2 changes: the target is a (tenant, dive) pair, and the selector takes the
oldest candidate across every tenant the orchestrator serves. The persist
records refusals as well as lengths (PLAN.md §9.16), and names the run.
Species names are read with the contracts' `parse_species_names`, v1's
definition of record, which the API package does not depend on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from temporalio import activity

from fishsense_services_api.laser_depth_store import Dot
from fishsense_services_api.measurement_store import (
    HeadTailPoints,
    LengthRecord,
    MeasurementCandidate,
    MeasurementWork,
    PersistedMeasurements,
)
from fishsense_services_contracts.laser_depth import LaserCalibrationGeometry, LaserDot
from fishsense_services_contracts.measurement import (
    HeadTail,
    MeasureFishCapture,
    MeasureFishInput,
    MeasureFishResult,
)
from fishsense_services_contracts.taxonomy import parse_species_names
from fishsense_services_orchestrator.laser_depth.activities import (
    DiveTarget,
    oldest_candidate,
    run_id,
)

__all__ = [
    "MeasurementActivities",
    "MeasurementCatalog",
    "MeasurementResolution",
    "PersistedMeasurementRun",
]


@dataclass(frozen=True)
class MeasurementResolution:
    """What the processor is handed, and v1's counters for what was skipped.
    `payload` is None when the dive has nothing to measure."""

    payload: MeasureFishInput | None
    skipped_already_measured: int
    skipped_unmeasurable_species: int
    missing_cluster: int
    missing_laser_or_headtail: int
    skipped_refused: int


@dataclass(frozen=True)
class PersistedMeasurementRun:
    measured: int
    refused: int
    skipped_stale: int
    fish_created: int
    clusters_bound: int


class MeasurementCatalog(Protocol):
    """What stage 14 asks the database; see
    ``fishsense_services_api.measurement_store.MeasurementCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_measurement(
        self, tenant_id: uuid.UUID
    ) -> MeasurementCandidate | None: ...

    async def measurement_work(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> MeasurementWork: ...

    async def persist_measurements(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, **result
    ) -> PersistedMeasurements: ...


class MeasurementActivities:
    def __init__(self, *, catalog: MeasurementCatalog) -> None:
        self._catalog = catalog

    @activity.defn(name="select_next_dive_for_measurement")
    async def select_next_dive_for_measurement(self) -> DiveTarget | None:
        """The oldest dive in the stage-14 cohort, across tenants."""
        best = await oldest_candidate(
            await self._catalog.member_tenants(),
            self._catalog.next_dive_for_measurement,
        )
        if best is None:
            activity.logger.info("no high-priority dives needing fish measurement")
        else:
            activity.logger.info(
                "next high-priority dive needing fish measurement: tenant=%s dive=%s",
                best.tenant_id,
                best.dive_id,
            )
        return best

    @activity.defn(name="resolve_measurement_inputs")
    async def resolve_measurement_inputs(
        self, target: DiveTarget
    ) -> MeasurementResolution:
        """The camera matrix, the effective laser calibration, and per capture
        to measure its one laser dot and one head/tail pair."""
        work = await self._catalog.measurement_work(target.tenant_id, target.dive_id)
        payload = None
        if work.geometry is not None and work.captures:
            geometry = work.geometry
            payload = MeasureFishInput(
                dive_id=target.dive_id,
                camera_matrix=geometry.camera_matrix,
                calibration=LaserCalibrationGeometry(
                    laser_calibration_id=geometry.laser_calibration_id,
                    laser_position=geometry.laser_position,
                    laser_axis=geometry.laser_axis,
                ),
                captures=[
                    MeasureFishCapture(
                        capture_id=item.capture_id,
                        species_label_id=item.species_label_id,
                        laser=LaserDot(
                            laser_label_id=item.laser.laser_label_id,
                            x=item.laser.x,
                            y=item.laser.y,
                        ),
                        head_tail=HeadTail(
                            head_tail_label_id=item.head_tail.head_tail_label_id,
                            head_x=item.head_tail.head_x,
                            head_y=item.head_tail.head_y,
                            tail_x=item.head_tail.tail_x,
                            tail_y=item.head_tail.tail_y,
                        ),
                    )
                    for item in work.captures
                ],
            )
        activity.logger.info(
            "resolved measurement inputs dive=%s captures=%d already_measured=%d "
            "unmeasurable_species=%d missing_cluster=%d missing_laser_or_headtail=%d "
            "refused=%d",
            target.dive_id,
            len(work.captures),
            work.skipped_already_measured,
            work.skipped_unmeasurable_species,
            work.missing_cluster,
            work.missing_laser_or_headtail,
            work.skipped_refused,
        )
        return MeasurementResolution(
            payload=payload,
            skipped_already_measured=work.skipped_already_measured,
            skipped_unmeasurable_species=work.skipped_unmeasurable_species,
            missing_cluster=work.missing_cluster,
            missing_laser_or_headtail=work.missing_laser_or_headtail,
            skipped_refused=work.skipped_refused,
        )

    @activity.defn(name="persist_measurements")
    async def persist_measurements(
        self,
        target: DiveTarget,
        laser_calibration_id: uuid.UUID,
        result: MeasureFishResult,
    ) -> PersistedMeasurementRun:
        """Bind each length to its fish and append it, or record its refusal --
        each only if it still answers the capture's open work."""
        lengths = [
            LengthRecord(
                capture_id=length.capture_id,
                species_label_id=length.species_label_id,
                laser=Dot(length.laser.laser_label_id, length.laser.x, length.laser.y),
                head_tail=HeadTailPoints(
                    length.head_tail.head_tail_label_id,
                    length.head_tail.head_x,
                    length.head_tail.head_y,
                    length.head_tail.tail_x,
                    length.head_tail.tail_y,
                ),
                length_m=length.length_m,
                depth_m=length.depth_m,
                refusal=length.refusal,
            )
            for length in result.captures
        ]
        persisted = await self._catalog.persist_measurements(
            target.tenant_id,
            target.dive_id,
            laser_calibration_id=laser_calibration_id,
            algorithm=result.algorithm,
            algorithm_version=result.algorithm_version,
            core_version=result.core_version,
            lengths=lengths,
            species_names=parse_species_names,
            run_id=run_id(),
        )
        activity.logger.info(
            "persisted measurements dive=%s measured=%d refused=%d stale=%d "
            "fish_created=%d clusters_bound=%d",
            target.dive_id,
            persisted.measured,
            persisted.refused,
            persisted.skipped_stale,
            persisted.fish_created,
            persisted.clusters_bound,
        )
        return PersistedMeasurementRun(
            measured=persisted.measured,
            refused=persisted.refused,
            skipped_stale=persisted.skipped_stale,
            fish_created=persisted.fish_created,
            clusters_bound=persisted.clusters_bound,
        )
