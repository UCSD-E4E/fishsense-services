"""Regroup a dive stage 6.1 already grouped: on demand, dry run by default.

New in v2 (2026-10-14). v1's stage 6.1 walked prediction clusters in no
stated order, so "Part of previous group" could join a cluster captured long
after the frame (dive 341, frame 101302) or fail to join the one just before
it (dive 465, frames 129275-6). v2 walks them in capture order, but refuses a
dive that already has label-studio clusters. This re-runs it:

* the plan is `ops.regroup.plan`: stage 6.1's clusters, and which fish each
  one is;
* **a dry run unless told otherwise**, reporting every measured frame whose
  fish would change and the plan's digest;
* **an apply names the digest it reviewed**, and is refused if the dive no
  longer plans to it; the store also refuses clusters changed between this
  plan and its write, under stage 6.1's per-dive lock;
* nothing is measured here. A frame whose cluster now points at another fish
  has a stale binding, and the hourly stage 14 measures it again on a
  high-priority dive.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.regroup_store import (
    ClustersChanged,
    NewCluster,
    RegroupFacts,
)
from fishsense_services_orchestrator.ops.checksums.activities import (
    DIVE_NOT_FOUND_TYPE,
)
from fishsense_services_orchestrator.ops.regroup.contracts import (
    RegroupMove,
    RegroupReport,
)
from fishsense_services_orchestrator.ops.regroup.plan import plan_regroup

__all__ = ["CLUSTERS_CHANGED_TYPE", "PLAN_MISMATCH_TYPE", "RegroupActivities"]

PLAN_MISMATCH_TYPE = "RegroupPlanMismatch"
CLUSTERS_CHANGED_TYPE = "ClustersChanged"


class RegroupCatalogLike(Protocol):
    """See ``fishsense_services_api.regroup_store.RegroupCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None: ...

    async def regroup_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> RegroupFacts: ...

    async def replace_label_studio_clusters(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        clusters: Sequence[NewCluster],
        *,
        expected_fingerprint: str,
    ) -> list[uuid.UUID | None]: ...


class RegroupActivities:
    def __init__(self, *, catalog: RegroupCatalogLike) -> None:
        self._catalog = catalog

    async def _find_dive(self, dive_number: int) -> tuple[uuid.UUID, uuid.UUID]:
        for tenant_id in await self._catalog.member_tenants():
            dive_id = await self._catalog.dive_by_number(tenant_id, dive_number)
            if dive_id is not None:
                return tenant_id, dive_id
        raise ApplicationError(
            f"no dive numbered {dive_number} in any tenant the orchestrator serves",
            type=DIVE_NOT_FOUND_TYPE,
            non_retryable=True,
        )

    @activity.defn(name="regroup_dive")
    async def regroup_dive(
        self,
        dive_number: int,
        apply: bool = False,
        expected_plan_sha256: str | None = None,
    ) -> RegroupReport:
        """Plan the dive's label-studio clusters afresh and (with ``apply``
        and the plan's digest) write them."""
        tenant_id, dive_id = await self._find_dive(dive_number)
        facts = await self._catalog.regroup_facts(tenant_id, dive_id)
        plan = plan_regroup(facts)
        report = RegroupReport(
            dive_number=dive_number,
            applied=False,
            noop=plan.noop,
            plan_sha256=plan.sha256,
            priority=facts.priority,
            clusters_before=len(facts.label_studio_clusters),
            clusters_after=len(plan.clusters),
            measured_frames=len(facts.measured),
            moves=[RegroupMove(**vars(m)) for m in plan.moves],
            warnings=plan.warnings,
        )
        if apply:
            if expected_plan_sha256 != plan.sha256:
                raise ApplicationError(
                    f"dive {dive_number} now plans to {plan.sha256}, not the "
                    f"reviewed {expected_plan_sha256}: dry-run it again",
                    type=PLAN_MISMATCH_TYPE,
                    non_retryable=True,
                )
            if not plan.noop:
                try:
                    await self._catalog.replace_label_studio_clusters(
                        tenant_id,
                        dive_id,
                        plan.clusters,
                        expected_fingerprint=facts.fingerprint,
                    )
                except ClustersChanged as exc:
                    raise ApplicationError(
                        str(exc), type=CLUSTERS_CHANGED_TYPE, non_retryable=True
                    ) from exc
            report.applied = True
        activity.logger.info(
            "regroup dive=%d applied=%s noop=%s clusters %d->%d moves=%d plan=%s",
            dive_number,
            report.applied,
            plan.noop,
            report.clusters_before,
            report.clusters_after,
            len(plan.moves),
            plan.sha256,
        )
        return report
