"""The render stage's orchestrator activities.

New in v2 (2026-10-19). Every canonical capture is owed a render: its raw
rectified to a JPEG, in its tenant's own bucket (`contracts.renders`).
Missions with a human measurement come first (`renders_store`). The
orchestrator issues the keys and, holding the tenant stores as the API does,
presigns a PUT per render for the processor, which holds no tenant
credentials. A tenant with no bucket gets no renders, and says so: there is no
shared fallback.

**Checked before recorded.** A presigned PUT signs no bytes, so the processor
reports the size and MD5 it wrote, and each is compared with the stored
object (its single-part ETag is its MD5) before the render is recorded. One
that does not match, or is not there, is not recorded and stays owed.

**The raw, copied in the same pass** (2026-10-20). What a capture owes is
judged in its tenant's *current* bucket (review of #151): a render, a copy of
its raw (``raws/{checksum}.ORF``), or both. A frame carries a presigned PUT
for each it owes; one rendered before raws were copied owes only the copy and
is not rendered again. A copy is recorded only if the stored object matches
the processor's report and its MD5 is the capture's own checksum -- a raw
changed on the NAS since ingest is not the capture's raw.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.renders_store import RawRow, RenderRow
from fishsense_services_api.tenant_stores import NoTenantStore
from fishsense_services_contracts.renders import (
    RENDER_VERSION,
    RenderCapturesInput,
    RenderFrame,
    RenderResult,
    raw_key,
    render_key,
)

__all__ = [
    "RENDER_PAGE",
    "RenderActivities",
    "RenderPage",
    "RenderPersistReport",
    "RenderTarget",
]


#: Owed captures per page: each carries a presigned PUT of ~600 characters, and
#: a page rides as an activity result, a child's input and the record's
#: argument -- a whole large mission at once would pass Temporal's 2 MB
#: payload limit (review of #144).
RENDER_PAGE = 500


@dataclass(frozen=True)
class RenderTarget:
    """A mission of a tenant."""

    tenant_id: uuid.UUID
    mission_id: uuid.UUID


@dataclass
class RenderPage:
    """A page of a mission's owed captures, and the cursor to the next."""

    payload: Optional[RenderCapturesInput]
    #: The last capture's number: the next page starts after it.
    last_number: Optional[int]
    no_bucket: bool = False


@dataclass
class RenderPersistReport:
    written: int = 0
    rendered: int = 0
    abstained: int = 0
    #: Rendered, but the stored object did not match what was reported.
    unverified: List[uuid.UUID] = field(default_factory=list)
    #: Raws copied and recorded (2026-10-20).
    raws: int = 0
    #: Copied, but the stored object or the capture's checksum disagreed.
    raws_unverified: List[uuid.UUID] = field(default_factory=list)


def _first(mission) -> tuple:
    """Human-measured missions first, then the oldest; then by id, stably."""
    created = mission.created_at
    return (
        not mission.human_measured,
        created is None,
        created.timestamp() if created is not None else 0.0,
        str(mission.mission_id),
    )


def _refuse(target: RenderTarget, why: str) -> ApplicationError:
    return ApplicationError(
        f"refusing the processor's renders for mission {target.mission_id}: {why}",
        type="InvalidRenders",
        non_retryable=True,
    )


class RenderActivities:
    def __init__(self, *, catalog: Any, store: Any, stores: Any) -> None:
        self._catalog = catalog
        #: The shared object store: where a mission's raws are staged.
        self._store = store
        #: The tenant stores (`TenantStores`), by slug.
        self._stores = stores

    async def _objects(self, tenant_id: uuid.UUID):
        """The tenant's bucket, or None when it has none."""
        slug = await self._catalog.tenant_slug(tenant_id)
        try:
            return self._stores.for_tenant(slug)
        except NoTenantStore:
            activity.logger.warning(
                "tenant %s has no bucket: its captures wait for their renders", slug
            )
            return None

    @activity.defn(name="select_render_missions")
    async def select_render_missions(
        self, limit: Optional[int] = None
    ) -> List[RenderTarget]:
        """Missions owing renders, of every tenant with a bucket: those with a
        human measurement first, then the oldest -- across tenants as within
        one (review of #144)."""
        found = []
        for tenant_id in await self._catalog.member_tenants():
            objects = await self._objects(tenant_id)
            if objects is None:
                continue
            for m in await self._catalog.render_missions(
                tenant_id,
                bucket=objects.bucket,
                render_version=RENDER_VERSION,
                limit=limit,
            ):
                found.append((tenant_id, m))
        found.sort(key=lambda tm: _first(tm[1]))
        chosen = found if limit is None else found[:limit]
        return [RenderTarget(t, m.mission_id) for t, m in chosen]

    @activity.defn(name="resolve_render_inputs")
    async def resolve_render_inputs(
        self,
        target: RenderTarget,
        after_number: Optional[int] = None,
        limit: int = RENDER_PAGE,
    ) -> RenderPage:
        """A page of the mission's owed captures -- those after
        `after_number` -- each with its staged raw and a presigned PUT for its
        render."""
        objects = await self._objects(target.tenant_id)
        if objects is None:
            return RenderPage(payload=None, last_number=None, no_bucket=True)
        inputs = await self._catalog.render_inputs(
            target.tenant_id,
            target.mission_id,
            bucket=objects.bucket,
            render_version=RENDER_VERSION,
            after_number=after_number,
            limit=limit,
        )
        frames = []
        for c in inputs.captures:
            owed: dict = {}
            if c.render_owed:
                key = render_key(
                    RENDER_VERSION, inputs.camera_calibration_id, c.checksum
                )
                owed.update(key=key, put_url=objects.presign_render_put(key).url)
            if c.raw_owed:
                copy = raw_key(c.checksum)
                owed.update(
                    checksum=c.checksum,
                    raw_key=copy,
                    raw_put_url=objects.presign_raw_put(copy).url,
                )
            frames.append(
                RenderFrame(
                    capture_id=c.capture_id,
                    raw=self._store.layout.raw(target.tenant_id, c.checksum),
                    **owed,
                )
            )
            activity.heartbeat()
        payload = RenderCapturesInput(
            tenant_id=target.tenant_id,
            mission_id=target.mission_id,
            render_version=RENDER_VERSION,
            camera_calibration_id=inputs.camera_calibration_id,
            camera_matrix=inputs.camera_matrix,
            distortion_coefficients=inputs.distortion_coefficients,
            frames=frames,
        )
        last = inputs.captures[-1].number if inputs.captures else None
        return RenderPage(payload=payload, last_number=last)

    @activity.defn(name="persist_renders")
    async def persist_renders(
        self,
        target: RenderTarget,
        payload: RenderCapturesInput,
        results: List[RenderResult],
    ) -> RenderPersistReport:
        """Record the renders the stored objects confirm, and the abstentions."""
        objects = await self._objects(target.tenant_id)
        if objects is None:
            raise _refuse(target, "the tenant has no bucket")
        asked = {f.capture_id: f for f in payload.frames}
        report = RenderPersistReport()
        rows: List[RenderRow] = []
        raws: List[RawRow] = []
        for r in results:
            frame = asked.get(r.capture_id)
            if frame is None:
                raise _refuse(target, f"capture {r.capture_id} was not asked for")
            if (r.render_version, r.camera_calibration_id) != (
                payload.render_version,
                payload.camera_calibration_id,
            ):
                raise _refuse(target, f"capture {r.capture_id}: another render")
            if r.status == "rendered" and not frame.owes_render:
                raise _refuse(target, f"capture {r.capture_id}: a render not owed")
            if r.raw_status is not None and not frame.owes_raw:
                raise _refuse(target, f"capture {r.capture_id}: a raw copy not owed")
            if r.raw_status == "copied":
                raw = await self._checked_raw(target, objects, frame, r, report)
                if raw is not None:
                    raws.append(raw)
            if r.status == "not_asked":
                continue
            if r.status != "rendered":
                report.abstained += 1
                rows.append(RenderRow(capture_id=r.capture_id, status=r.status,
                                      render_version=r.render_version,
                                      camera_calibration_id=r.camera_calibration_id))  # fmt: skip
                continue
            stored = await asyncio.to_thread(objects.head, frame.key)
            if stored is None or stored.size != r.bytes or stored.md5 != r.md5:
                activity.logger.warning(
                    "mission=%s capture=%s: stored render %s does not match the "
                    "processor's (%s bytes, md5 %s); left owed",
                    target.mission_id, r.capture_id, stored, r.bytes, r.md5,
                )  # fmt: skip
                report.unverified.append(r.capture_id)
                continue
            report.rendered += 1
            rows.append(
                RenderRow(
                    capture_id=r.capture_id, status="rendered",
                    render_version=r.render_version,
                    camera_calibration_id=r.camera_calibration_id,
                    bucket=objects.bucket, key=frame.key, bytes=r.bytes, md5=r.md5,
                    width=r.width, height=r.height,
                )  # fmt: skip
            )
        report.written = await self._catalog.persist_renders(
            target.tenant_id, target.mission_id, rows
        )
        report.raws = await self._catalog.persist_raws(
            target.tenant_id, target.mission_id, raws
        )
        activity.logger.info(
            "mission=%s renders written=%d rendered=%d abstained=%d unverified=%d "
            "raws=%d raws_unverified=%d",
            target.mission_id, report.written, report.rendered, report.abstained,
            len(report.unverified), report.raws, len(report.raws_unverified),
        )  # fmt: skip
        return report

    async def _checked_raw(
        self, target: RenderTarget, objects, frame: RenderFrame, r: RenderResult,
        report: RenderPersistReport,
    ) -> Optional[RawRow]:  # fmt: skip
        """The copy, if the stored object is what the processor reported and
        that is the capture's raw (its checksum); else None, left owed."""
        stored = await asyncio.to_thread(objects.head, frame.raw_key)
        if (
            stored is None
            or stored.size != r.raw_bytes
            or stored.md5 != r.raw_md5
            or r.raw_md5 != frame.checksum
        ):
            activity.logger.warning(
                "mission=%s capture=%s: raw copy %s does not match (stored %s; "
                "reported %s bytes, md5 %s; capture checksum %s); left owed",
                target.mission_id, r.capture_id, frame.raw_key, stored, r.raw_bytes,
                r.raw_md5, frame.checksum,
            )  # fmt: skip
            report.raws_unverified.append(r.capture_id)
            return None
        return RawRow(capture_id=r.capture_id, bucket=objects.bucket,
                      key=frame.raw_key, bytes=r.raw_bytes, md5=r.raw_md5)  # fmt: skip
