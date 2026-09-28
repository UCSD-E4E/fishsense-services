"""The laser-depth stage's orchestrator activities: select, resolve, persist.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
select_next_high_priority_dive_for_laser_depth_activity.py (and the shared
`cohort_selection.select_next_dive`), plus the read and write halves of the
data-worker's compute_laser_depths_activity.py (`load_dive_calibration_context`,
the labels and depths it read, the `put_laser_depth` it wrote). v1's selector
was one SDK call; v2's call the laser-depth catalog
(`fishsense_services_api.laser_depth_store`), which owns the cohort, the work
and the persistence rules. The processor only computes.

v2 changes: the target is a (tenant, dive) pair, and the selector takes the
oldest candidate across every tenant the orchestrator serves (v1's first in,
first out, kept across tenants so none can starve another). The persist
records refusals as well as depths (PLAN.md §9.16).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from temporalio import activity

from fishsense_services_api.laser_depth_store import (
    DepthRecord,
    DepthRefusal,
    LaserDepthCandidate,
    LaserDepthWork,
    PersistedDepths,
)
from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsInput,
    ComputeLaserDepthsResult,
    LaserCalibrationGeometry,
    LaserDepthCapture,
    LaserDot,
)

__all__ = [
    "DiveTarget",
    "LaserDepthActivities",
    "LaserDepthCatalog",
    "LaserDepthResolution",
    "PersistedLaserDepths",
    "oldest_candidate",
    "run_id",
]


@dataclass(frozen=True)
class DiveTarget:
    tenant_id: uuid.UUID
    dive_id: uuid.UUID


@dataclass(frozen=True)
class LaserDepthResolution:
    """What the processor is handed, and v1's counters for what was skipped.
    `payload` is None when the dive has nothing to compute."""

    payload: ComputeLaserDepthsInput | None
    skipped_current: int
    skipped_refused: int
    skipped_unusable_label: int


@dataclass(frozen=True)
class PersistedLaserDepths:
    computed: int
    refused: int
    skipped_stale: int


class LaserDepthCatalog(Protocol):
    """What the laser-depth stage asks the database; see
    ``fishsense_services_api.laser_depth_store.LaserDepthCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_laser_depth(
        self, tenant_id: uuid.UUID
    ) -> LaserDepthCandidate | None: ...

    async def laser_depth_work(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> LaserDepthWork: ...

    async def persist_laser_depths(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, **result
    ) -> PersistedDepths: ...


async def oldest_candidate(tenants, next_for_tenant) -> DiveTarget | None:
    """The oldest candidate across the tenants: v1's `ORDER BY id LIMIT 1`,
    kept first in, first out across tenants (the clustering selector's rule)."""
    best: DiveTarget | None = None
    best_key = None
    for tenant_id in tenants:
        candidate = await next_for_tenant(tenant_id)
        if candidate is None:
            continue
        key = (candidate.created_at, str(candidate.dive_id))
        if best_key is None or key < best_key:
            best, best_key = DiveTarget(tenant_id, candidate.dive_id), key
    return best


def run_id() -> uuid.UUID | None:
    """The workflow run this activity serves, as a result's provenance."""
    try:
        return uuid.UUID(activity.info().workflow_run_id)
    except (TypeError, ValueError):
        return None


class LaserDepthActivities:
    def __init__(self, *, catalog: LaserDepthCatalog) -> None:
        self._catalog = catalog

    @activity.defn(name="select_next_dive_for_laser_depth")
    async def select_next_dive_for_laser_depth(self) -> DiveTarget | None:
        """The oldest dive in the laser-depth cohort, across tenants."""
        best = await oldest_candidate(
            await self._catalog.member_tenants(),
            self._catalog.next_dive_for_laser_depth,
        )
        if best is None:
            activity.logger.info("no high-priority dives needing laser depths")
        else:
            activity.logger.info(
                "next high-priority dive needing laser depths: tenant=%s dive=%s",
                best.tenant_id,
                best.dive_id,
            )
        return best

    @activity.defn(name="resolve_laser_depth_inputs")
    async def resolve_laser_depth_inputs(
        self, target: DiveTarget
    ) -> LaserDepthResolution:
        """The camera matrix, the effective laser calibration, and per capture
        needing a depth its valid labels in the order to try them."""
        work = await self._catalog.laser_depth_work(target.tenant_id, target.dive_id)
        payload = None
        if work.geometry is not None and work.captures:
            geometry = work.geometry
            payload = ComputeLaserDepthsInput(
                dive_id=target.dive_id,
                camera_matrix=geometry.camera_matrix,
                calibration=LaserCalibrationGeometry(
                    laser_calibration_id=geometry.laser_calibration_id,
                    laser_position=geometry.laser_position,
                    laser_axis=geometry.laser_axis,
                ),
                captures=[
                    LaserDepthCapture(
                        capture_id=capture.capture_id,
                        laser_labels=[
                            LaserDot(laser_label_id=d.laser_label_id, x=d.x, y=d.y)
                            for d in capture.laser_labels
                        ],
                    )
                    for capture in work.captures
                ],
            )
        activity.logger.info(
            "resolved laser-depth inputs dive=%s captures=%d skipped_current=%d "
            "skipped_refused=%d skipped_unusable_label=%d",
            target.dive_id,
            len(work.captures),
            work.skipped_current,
            work.skipped_refused,
            work.skipped_unusable_label,
        )
        return LaserDepthResolution(
            payload=payload,
            skipped_current=work.skipped_current,
            skipped_refused=work.skipped_refused,
            skipped_unusable_label=work.skipped_unusable_label,
        )

    @activity.defn(name="persist_laser_depths")
    async def persist_laser_depths(
        self,
        target: DiveTarget,
        laser_calibration_id: uuid.UUID,
        result: ComputeLaserDepthsResult,
    ) -> PersistedLaserDepths:
        """Append the processor's depths and refusals, each only if it still
        answers the dive's open work."""
        depths = []
        refusals = []
        for outcome in result.captures:
            refusals += [
                DepthRefusal(
                    capture_id=outcome.capture_id,
                    laser_label_id=r.laser_label_id,
                    x=r.x,
                    y=r.y,
                    reason=r.reason,
                    depth_m=r.depth_m,
                )
                for r in outcome.refusals
            ]
            if outcome.depth is not None:
                depths.append(
                    DepthRecord(
                        capture_id=outcome.capture_id,
                        laser_label_id=outcome.depth.laser_label_id,
                        depth_m=outcome.depth.depth_m,
                        range_m=outcome.depth.range_m,
                        residual_m=outcome.depth.residual_m,
                    )
                )
        persisted = await self._catalog.persist_laser_depths(
            target.tenant_id,
            target.dive_id,
            laser_calibration_id=laser_calibration_id,
            core_version=result.core_version,
            depths=depths,
            refusals=refusals,
            run_id=run_id(),
        )
        activity.logger.info(
            "persisted laser depths dive=%s computed=%d refused=%d stale=%d",
            target.dive_id,
            persisted.written,
            persisted.refused,
            persisted.skipped_stale,
        )
        return PersistedLaserDepths(
            computed=persisted.written,
            refused=persisted.refused,
            skipped_stale=persisted.skipped_stale,
        )
