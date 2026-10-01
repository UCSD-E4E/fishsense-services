"""Stage 1's orchestrator activities: select, resolve, persist.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(select_next_high_priority_dive_for_clustering_activity.py,
resolve_dive_frame_clustering_inputs_activity.py,
persist_dive_frame_clusters_activity.py). v1's were thin SDK calls; v2's call
the clustering catalog (``fishsense_services_api.clustering_store``), which
owns the cohort and the persistence rules.

v2 changes: the target is a (tenant, dive) pair, and the selector takes the
oldest candidate across every tenant the orchestrator serves -- v1's first in,
first out, kept across tenants so none can starve another.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.clustering_store import (
    ClusteringCandidate,
    InvalidClusters,
)
from fishsense_services_contracts import ClusterDiveFrameImage, ClusterDiveFramesInput

__all__ = ["ClusteringActivities", "ClusteringCatalog", "ClusteringTarget"]


@dataclass(frozen=True)
class ClusteringTarget:
    tenant_id: uuid.UUID
    dive_id: uuid.UUID


class ClusteringCatalog(Protocol):
    """What stage 1 asks the database; see
    ``fishsense_services_api.clustering_store.ClusteringCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_clustering(
        self, tenant_id: uuid.UUID
    ) -> ClusteringCandidate | None: ...

    async def canonical_capture_times(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, datetime]]: ...

    async def persist_prediction_clusters(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, clusters: list[list[uuid.UUID]]
    ) -> int: ...


class ClusteringActivities:
    def __init__(self, *, catalog: ClusteringCatalog) -> None:
        self._catalog = catalog

    @activity.defn(name="select_next_dive_for_clustering")
    async def select_next_dive_for_clustering(self) -> ClusteringTarget | None:
        """The oldest dive in the stage-1 cohort, across tenants."""
        best: ClusteringTarget | None = None
        best_key = None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_clustering(tenant_id)
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = ClusteringTarget(tenant_id, candidate.dive_id), key
        if best is None:
            activity.logger.info("no high-priority dives needing dive-frame clustering")
        else:
            activity.logger.info(
                "next high-priority dive needing dive-frame clustering: "
                "tenant=%s dive=%s",
                best.tenant_id,
                best.dive_id,
            )
        return best

    @activity.defn(name="resolve_clustering_inputs")
    async def resolve_clustering_inputs(
        self, target: ClusteringTarget
    ) -> ClusterDiveFramesInput:
        """The dive's canonical captures and times: all the kernel reads."""
        captures = await self._catalog.canonical_capture_times(
            target.tenant_id, target.dive_id
        )
        activity.logger.info(
            "resolved clustering inputs dive=%s captures=%d",
            target.dive_id,
            len(captures),
        )
        return ClusterDiveFramesInput(
            dive_id=target.dive_id,
            images=[
                ClusterDiveFrameImage(capture_id=capture, taken_datetime=taken)
                for capture, taken in captures
            ],
        )

    @activity.defn(name="persist_prediction_clusters")
    async def persist_prediction_clusters(
        self, target: ClusteringTarget, clusters: List[List[uuid.UUID]]
    ) -> int:
        """Write the dive's prediction clusters, all or nothing. A refusal is
        final: retrying re-reads the same clusters to the same conclusion."""
        try:
            written = await self._catalog.persist_prediction_clusters(
                target.tenant_id, target.dive_id, clusters
            )
        except InvalidClusters as exc:
            raise ApplicationError(
                f"refusing the processor's clusters for dive {target.dive_id}: {exc}",
                type="InvalidClusters",
                non_retryable=True,
            ) from exc
        activity.logger.info(
            "persisted %d prediction clusters for dive=%s", written, target.dive_id
        )
        return written
