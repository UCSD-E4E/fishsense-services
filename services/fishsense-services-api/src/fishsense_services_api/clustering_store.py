"""The database side of stage 1 (dive-frame clustering), tenant-scoped.

Ported from fishsense-lite@a8b2c3bc: the cohort is the API's
`select-next/dive-frame-clustering` endpoint (dive_cohort_controller.py), the
inputs are `resolve_dive_frame_clustering_inputs_activity`, and the write is
`persist_dive_frame_clusters_activity`. The cohort is v1's: a high-priority
dive with a valid laser label (completed, not superseded, x and y set) on a
canonical capture, and no prediction cluster yet.

v2 changes:

* per tenant, ordered by `created_at` (v1: `id`, first in first out) so the
  orchestrator can take the oldest candidate across the tenants it serves;
* **persisting is all-or-nothing** and serialised per dive. v1 posted clusters
  one at a time, so a failure mid-persist left a partial set its own cohort
  gate then skipped forever, until an operator deleted the rows by hand;
* **the processor's output is checked** (PLAN.md §9.11): every capture must be
  a canonical capture of the dive, in exactly one cluster.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "ClusteringCandidate",
    "ClusteringCatalog",
    "ForeignCapture",
    "InvalidClusters",
    "canonical_capture_times",
    "next_dive_for_clustering",
    "persist_prediction_clusters",
]

#: The repo-wide *valid* laser label (v1's `_valid_laser_conditions`): the
#: labeler placed a point, the validator signed off, and no RANSAC fit has
#: superseded it. Stages 1, 2, 5.1 and 14 all cascade from it.
VALID_LASER = "l.completed AND NOT l.superseded AND l.x IS NOT NULL AND l.y IS NOT NULL"


class InvalidClusters(ValueError):
    """The clusters do not partition the dive's canonical captures."""


class ForeignCapture(InvalidClusters):
    """A capture that is not a canonical capture of the dive."""


@dataclass(frozen=True)
class ClusteringCandidate:
    dive_id: uuid.UUID
    created_at: datetime


async def next_dive_for_clustering(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> ClusteringCandidate | None:
    """The tenant's oldest dive in the stage-1 cohort."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM dives d
                WHERE d.tenant_id = :tenant AND d.priority = 'high'
                  AND EXISTS (
                      SELECT 1 FROM laser_labels l
                      JOIN captures c
                        ON c.tenant_id = l.tenant_id AND c.id = l.capture_id
                      WHERE c.tenant_id = d.tenant_id AND c.dive_id = d.id
                        AND c.is_canonical AND {VALID_LASER}
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM dive_frame_clusters k
                      WHERE k.tenant_id = d.tenant_id AND k.dive_id = d.id
                        AND k.formed_by = 'prediction'
                  )
                ORDER BY d.created_at, d.id
                LIMIT 1
                """),
            {"tenant": tenant_id},
        )
    ).one_or_none()
    return None if row is None else ClusteringCandidate(row.id, row.created_at)


async def canonical_capture_times(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[tuple[uuid.UUID, datetime]]:
    """The dive's canonical captures and their times, in time order. Canonical
    only, mirroring the cohort, or the work dispatched would not match what the
    cohort promised and the dive could never drain."""
    rows = await conn.execute(
        text("""
            SELECT id, captured_at FROM captures
            WHERE tenant_id = :tenant AND dive_id = :dive AND is_canonical
            ORDER BY captured_at, id
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [(r.id, r.captured_at) for r in rows]


async def persist_prediction_clusters(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    clusters: list[list[uuid.UUID]],
) -> int:
    """Write the dive's prediction clusters, all or nothing, in the caller's
    transaction. Returns how many were written; 0 if the dive already has
    prediction clusters (a retry after a lost acknowledgement). Empty groups
    are skipped, as in v1."""
    # Serialise persists of one dive, so two can't both find "none yet".
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"prediction-clusters:{dive_id}"},
    )
    if (
        await conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM dive_frame_clusters
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND formed_by = 'prediction'
                )
                """),
            {"tenant": tenant_id, "dive": dive_id},
        )
    ).scalar_one():
        return 0

    groups = [group for group in clusters if group]
    members = [capture for group in groups for capture in group]
    if len(members) != len(set(members)):
        raise InvalidClusters(f"a capture of dive {dive_id} is in two clusters")
    canonical = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM captures
                    WHERE tenant_id = :tenant AND dive_id = :dive
                      AND is_canonical AND id = ANY(:ids)
                    """),
                {"tenant": tenant_id, "dive": dive_id, "ids": members},
            )
        ).scalars()
    )
    if foreign := set(members) - canonical:
        raise ForeignCapture(
            f"not canonical captures of dive {dive_id}: {sorted(map(str, foreign))}"
        )

    for group in groups:
        cluster_id = (
            await conn.execute(
                text("""
                    INSERT INTO dive_frame_clusters
                        (tenant_id, dive_id, formed_by, updated_at)
                    VALUES (:tenant, :dive, 'prediction', now())
                    RETURNING id
                    """),
                {"tenant": tenant_id, "dive": dive_id},
            )
        ).scalar_one()
        await conn.execute(
            text("""
                INSERT INTO dive_frame_cluster_captures
                    (tenant_id, cluster_id, capture_id)
                SELECT :tenant, :cluster, unnest(CAST(:captures AS uuid[]))
                """),
            {"tenant": tenant_id, "cluster": cluster_id, "captures": group},
        )
    return len(groups)


class ClusteringCatalog(ServicePrincipal):
    """Stage 1's database side, as the orchestrator's service principal."""

    async def next_dive_for_clustering(
        self, tenant_id: uuid.UUID
    ) -> ClusteringCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_dive_for_clustering(conn, tenant_id)

    async def canonical_capture_times(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, datetime]]:
        async with self._tenant(tenant_id) as conn:
            return await canonical_capture_times(conn, tenant_id, dive_id)

    async def persist_prediction_clusters(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, clusters: list[list[uuid.UUID]]
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_prediction_clusters(conn, tenant_id, dive_id, clusters)
