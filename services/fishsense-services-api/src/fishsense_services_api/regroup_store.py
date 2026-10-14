"""The database side of re-grouping a dive stage 6.1 already grouped,
tenant-scoped.

New in v2 (2026-10-14). v1's stage 6.1 walked a dive's prediction clusters in
no stated order, so a labeller's "Part of previous group" joined whatever
cluster the database returned before it, not the one captured before it. On
dive 341 that put Hogfish frame 101302 with two Stoplight Parrotfish frames
half an hour later, and on dive 465 it split one parrotfish into animals 330
and 331. v2's stage 6.1 walks captures in order, but refuses a dive that
already has label-studio clusters, and every migrated dive has them.

The orchestrator's regroup (``species.regroup``) plans the new clusters from
the labels and decides which fish each one is; this store reads what it plans
from and makes the one write. The write:

* replaces the dive's label-studio clusters -- and nothing else -- all or
  nothing, under stage 6.1's per-dive lock;
* only if they are still the clusters the plan was made from
  (``clusters_fingerprint``), so a plan reviewed in a dry run cannot be
  applied over a change made since;
* binds each new cluster to the fish the plan names, a new fish of the named
  species, or nothing (a cluster with no measured frame: stage 14 binds it
  when it first measures one).

Measurements are append-only and are not touched. A frame whose cluster now
points at another fish has a stale binding (migration 0028), so stage 14
measures it again under that fish -- on a high-priority dive, which is the
only kind stage 14 measures.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.checksum_store import dive_by_number
from fishsense_services_api.clustering_store import ForeignCapture
from fishsense_services_api.service_principal import ServicePrincipal
from fishsense_services_api.species_store import (
    SpeciesLabelRow,
    species_grouping_facts,
)

__all__ = [
    "ClustersChanged",
    "LabelStudioCluster",
    "MeasuredCapture",
    "NewCluster",
    "RegroupCatalog",
    "RegroupFacts",
    "clusters_fingerprint",
    "regroup_facts",
    "replace_label_studio_clusters",
]


class ClustersChanged(Exception):
    """The dive's label-studio clusters are not the ones the plan was made from."""


@dataclass(frozen=True)
class LabelStudioCluster:
    id: uuid.UUID
    number: int
    fish_id: uuid.UUID | None
    #: In capture order.
    members: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class MeasuredCapture:
    """A current server measurement of a real fish on the dive."""

    capture_id: uuid.UUID
    #: v1's image id, for a migrated capture.
    capture_number: int
    fish_id: uuid.UUID
    #: v1's fish id, for a migrated fish.
    fish_number: int
    fish_scientific_name: str | None
    length_m: float


@dataclass(frozen=True)
class RegroupFacts:
    priority: str
    #: Capture ids per prediction cluster, clusters and members in capture order.
    prediction_clusters: list[list[uuid.UUID]]
    #: The live species labels, sentinels included (stage 6.1's input).
    species_labels: list[SpeciesLabelRow]
    label_studio_clusters: list[LabelStudioCluster]
    measured: list[MeasuredCapture]
    #: ``clusters_fingerprint(label_studio_clusters)``.
    fingerprint: str
    #: Every capture of the dive's number, for the report.
    capture_numbers: dict[uuid.UUID, int]


@dataclass(frozen=True)
class NewCluster:
    """One label-studio cluster to write, and which fish it is."""

    #: In capture order.
    captures: Sequence[uuid.UUID]
    #: An existing real fish of the tenant.
    fish_id: uuid.UUID | None = None
    #: (common, scientific) of a new fish to create for it.
    new_fish_names: tuple[str, str] | None = None


def clusters_fingerprint(clusters: Sequence[LabelStudioCluster]) -> str:
    """Membership and binding, independent of row order and ids."""
    canonical = sorted(
        [str(k.fish_id or ""), sorted(str(c) for c in k.members)] for k in clusters
    )
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


async def _label_studio_clusters(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> list[LabelStudioCluster]:
    rows = await conn.execute(
        text("""
            SELECT k.id, k.number, k.fish_id,
                   array_agg(c.id ORDER BY c.captured_at, c.number) AS members
            FROM dive_frame_clusters k
            JOIN dive_frame_cluster_captures m
              ON m.tenant_id = k.tenant_id AND m.cluster_id = k.id
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            WHERE k.tenant_id = :tenant AND k.dive_id = :dive
              AND k.formed_by = 'label_studio'
            GROUP BY k.id
            ORDER BY min(c.captured_at), min(c.number), k.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    return [
        LabelStudioCluster(r.id, r.number, r.fish_id, tuple(r.members)) for r in rows
    ]


async def regroup_facts(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> RegroupFacts:
    """What a regroup plans from."""
    grouping = await species_grouping_facts(conn, tenant_id, dive_id)
    clusters = await _label_studio_clusters(conn, tenant_id, dive_id)
    priority = (
        await conn.execute(
            text("SELECT priority FROM dives WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": dive_id},
        )
    ).scalar_one()
    measured = await conn.execute(
        text("""
            SELECT m.capture_id, c.number AS capture_number, m.fish_id,
                   f.number AS fish_number, s.scientific_name, m.length_m
            FROM current_measurements m
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            JOIN fish f ON f.tenant_id = m.tenant_id AND f.id = m.fish_id
            LEFT JOIN species s ON s.id = f.species_id
            WHERE m.tenant_id = :tenant AND c.dive_id = :dive
              AND m.source = 'server' AND f.species_id IS NOT NULL
            ORDER BY c.captured_at, c.number, f.number
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    numbers = await conn.execute(
        text("SELECT id, number FROM captures WHERE tenant_id = :t AND dive_id = :d"),
        {"t": tenant_id, "d": dive_id},
    )
    return RegroupFacts(
        priority=priority,
        prediction_clusters=grouping.prediction_clusters,
        species_labels=grouping.species_labels,
        label_studio_clusters=clusters,
        measured=[
            MeasuredCapture(
                r.capture_id,
                r.capture_number,
                r.fish_id,
                r.fish_number,
                r.scientific_name,
                r.length_m,
            )
            for r in measured
        ],
        fingerprint=clusters_fingerprint(clusters),
        capture_numbers={r.id: r.number for r in numbers},
    )


async def _new_fish(
    conn: AsyncConnection, tenant_id: uuid.UUID, names: tuple[str, str]
) -> uuid.UUID:
    common, scientific = names
    species_id = (
        await conn.execute(
            text("SELECT ensure_species(:scientific, :common)"),
            {"scientific": scientific, "common": common},
        )
    ).scalar_one()
    return (
        await conn.execute(
            text("""
                INSERT INTO fish (tenant_id, species_id) VALUES (:tenant, :species)
                RETURNING id
                """),
            {"tenant": tenant_id, "species": species_id},
        )
    ).scalar_one()


async def replace_label_studio_clusters(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    dive_id: uuid.UUID,
    clusters: Sequence[NewCluster],
    *,
    expected_fingerprint: str,
) -> list[uuid.UUID | None]:
    """Replace the dive's label-studio clusters, in the caller's transaction.
    Returns each new cluster's fish (None: unbound, or left with no canonical
    capture and not written)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"label-studio-clusters:{dive_id}"},
    )
    current = await _label_studio_clusters(conn, tenant_id, dive_id)
    if clusters_fingerprint(current) != expected_fingerprint:
        raise ClustersChanged(
            f"dive {dive_id}'s label-studio clusters changed since the plan"
        )

    members = {c for cluster in clusters for c in cluster.captures}
    canonical = {
        r.id: r.is_canonical
        for r in await conn.execute(
            text("""
                SELECT id, is_canonical FROM captures
                WHERE tenant_id = :tenant AND dive_id = :dive AND id = ANY(:ids)
                """),
            {"tenant": tenant_id, "dive": dive_id, "ids": list(members)},
        )
    }
    if foreign := members - canonical.keys():
        raise ForeignCapture(
            f"not captures of dive {dive_id}: {sorted(map(str, foreign))}"
        )
    named = {k.fish_id for k in clusters if k.fish_id is not None}
    real = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM fish WHERE tenant_id = :tenant AND id = ANY(:ids)
                      AND species_id IS NOT NULL
                    """),
                {"tenant": tenant_id, "ids": list(named)},
            )
        ).scalars()
    )
    if unknown := named - real:
        raise ValueError(f"not real fish of the tenant: {sorted(map(str, unknown))}")

    await conn.execute(
        text("""
            DELETE FROM dive_frame_clusters
            WHERE tenant_id = :tenant AND dive_id = :dive AND formed_by = 'label_studio'
            """),
        {"tenant": tenant_id, "dive": dive_id},
    )
    bound: list[uuid.UUID | None] = []
    for cluster in clusters:
        kept = [c for c in cluster.captures if canonical[c]]
        if not kept:
            bound.append(None)
            continue
        fish_id = cluster.fish_id
        if fish_id is None and cluster.new_fish_names is not None:
            fish_id = await _new_fish(conn, tenant_id, cluster.new_fish_names)
        cluster_id = (
            await conn.execute(
                text("""
                    INSERT INTO dive_frame_clusters
                        (tenant_id, dive_id, formed_by, fish_id, updated_at)
                    VALUES (:tenant, :dive, 'label_studio', :fish, now())
                    RETURNING id
                    """),
                {"tenant": tenant_id, "dive": dive_id, "fish": fish_id},
            )
        ).scalar_one()
        await conn.execute(
            text("""
                INSERT INTO dive_frame_cluster_captures
                    (tenant_id, cluster_id, capture_id)
                SELECT :tenant, :cluster, unnest(CAST(:captures AS uuid[]))
                """),
            {"tenant": tenant_id, "cluster": cluster_id, "captures": kept},
        )
        bound.append(fish_id)
    return bound


class RegroupCatalog(ServicePrincipal):
    """A regroup's database side, as the orchestrator's principal."""

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None:
        async with self._tenant(tenant_id) as conn:
            return await dive_by_number(conn, tenant_id, number)

    async def regroup_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> RegroupFacts:
        async with self._tenant(tenant_id) as conn:
            return await regroup_facts(conn, tenant_id, dive_id)

    async def replace_label_studio_clusters(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        clusters: Sequence[NewCluster],
        *,
        expected_fingerprint: str,
    ) -> list[uuid.UUID | None]:
        async with self._tenant(tenant_id) as conn:
            return await replace_label_studio_clusters(
                conn,
                tenant_id,
                dive_id,
                clusters,
                expected_fingerprint=expected_fingerprint,
            )
