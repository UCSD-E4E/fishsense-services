"""The automatic track over HTTP, for review (the owner, 2026-10-21: "use
this app to review the auto process").

Read-only, for any member of the tenant; the web shows it as automatic and
unvalidated. The rules are in ``automatic_review_store``. Here: a frame's
render and mask go out as presigned links when they are in the tenant's
current bucket, and a frame or mission the caller can't see, or the run never
reached, is 404.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.automatic_review_store import (
    automatic_frame,
    mission_automatic,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.tenant_stores import NoTenantStore, TenantStores

__all__ = ["add_automatic_routes"]


class PixelPoint(BaseModel):
    x: float
    y: float


class AutomaticFrameView(BaseModel):
    #: ``predicted``, or why the run abstained.
    status: str
    predicted_at: datetime
    #: Full-frame pixels of ``render_url``'s image.
    laser: PixelPoint | None
    laser_confidence: float | None
    head: PixelPoint | None
    tail: PixelPoint | None
    mask_bbox: list[int] | None
    sam_score: float | None
    slate_probability: float | None
    predictor_version: int
    checkpoint: str | None
    core_version: str | None
    render_url: str | None
    #: Named by a review of these points (0062).
    render_id: uuid.UUID | None
    render_width: int | None
    render_height: int | None
    #: A frame-sized greyscale PNG, 255 on the fish (0059).
    mask_url: str | None
    mask_width: int | None
    mask_height: int | None
    length_m: float | None
    depth_m: float | None
    refusal: str | None
    calibration_source: str | None
    measurement_algorithm: str | None
    measurement_algorithm_version: str | None
    species: str | None
    species_probability: float | None
    species_margin: float | None
    species_top5: list[dict]


class AutomaticCalibrationView(BaseModel):
    outcome: str
    refusal_reason: str | None
    algorithm_version: str
    frames_used: int | None
    pair_count: int | None
    size_ratio: float | None
    se_px: float | None
    created_at: datetime


class AutomaticFailureView(BaseModel):
    step: str
    error: str
    created_at: datetime


class MissionAutomaticView(BaseModel):
    frames: int
    by_status: dict[str, int]
    unprocessed: int
    calibration: AutomaticCalibrationView | None
    lengths_use: str | None
    lengths: int
    refusals: dict[str, int]
    failures: list[AutomaticFailureView]
    excluded: str | None
    #: Agreement with the human path on the same frames, not error against a
    #: known length.
    compared: int
    median_error_pct: float | None
    mean_abs_error_pct: float | None
    within_5_pct: int
    within_10_pct: int


def _point(p) -> PixelPoint | None:
    return None if p is None else PixelPoint(x=p[0], y=p[1])


def add_automatic_routes(
    app: FastAPI, *, engine: AsyncEngine, membership, stores: TenantStores
) -> None:
    Member = Annotated[Membership, Depends(membership)]

    def link(slug: str, bucket: str | None, key: str | None) -> str | None:
        """A presigned GET, when the object is in the tenant's current bucket."""
        if bucket is None or key is None:
            return None
        try:
            objects = stores.for_tenant(slug)
        except NoTenantStore:
            return None
        return objects.presign_get(key) if objects.bucket == bucket else None

    @app.get(
        "/tenants/{slug}/captures/{capture_id}/automatic",
        operation_id="get_automatic_frame",
        response_model=AutomaticFrameView,
    )
    async def get_frame(
        slug: str, capture_id: uuid.UUID, member: Member
    ) -> AutomaticFrameView:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await automatic_frame(conn, member.tenant_id, capture_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no automatic result")
        return AutomaticFrameView(
            status=found.status,
            predicted_at=found.created_at,
            laser=_point(found.laser),
            laser_confidence=found.laser_confidence,
            head=_point(found.head),
            tail=_point(found.tail),
            mask_bbox=found.mask_bbox,
            sam_score=found.sam_score,
            slate_probability=found.slate_probability,
            predictor_version=found.predictor_version,
            checkpoint=found.checkpoint,
            core_version=found.core_version,
            render_url=link(slug, found.render_bucket, found.render_key),
            render_id=found.render_id,
            render_width=found.render_width,
            render_height=found.render_height,
            mask_url=link(slug, found.mask_bucket, found.mask_key),
            mask_width=found.mask_width,
            mask_height=found.mask_height,
            length_m=found.length_m,
            depth_m=found.depth_m,
            refusal=found.refusal,
            calibration_source=found.calibration_source,
            measurement_algorithm=found.measurement_algorithm,
            measurement_algorithm_version=found.measurement_algorithm_version,
            species=found.species,
            species_probability=found.species_probability,
            species_margin=found.species_margin,
            species_top5=found.species_top5,
        )

    @app.get(
        "/tenants/{slug}/missions/{number}/automatic",
        operation_id="get_mission_automatic",
        response_model=MissionAutomaticView,
    )
    async def get_mission(
        number: Annotated[int, Path(ge=0)], member: Member
    ) -> MissionAutomaticView:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            mission_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM missions WHERE tenant_id = :t AND number = :n"
                    ),
                    {"t": member.tenant_id, "n": number},
                )
            ).scalar_one_or_none()
            if mission_id is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "no such mission")
            run = await mission_automatic(conn, member.tenant_id, mission_id)
        return MissionAutomaticView(
            frames=run.frames,
            by_status=run.by_status,
            unprocessed=run.unprocessed,
            calibration=(
                None
                if run.calibration is None
                else AutomaticCalibrationView(**vars(run.calibration))
            ),
            lengths_use=run.lengths_use,
            lengths=run.lengths,
            refusals=run.refusals,
            failures=[AutomaticFailureView(**vars(f)) for f in run.failures],
            excluded=run.excluded,
            compared=run.compared,
            median_error_pct=run.median_error_pct,
            mean_abs_error_pct=run.mean_abs_error_pct,
            within_5_pct=run.within_5_pct,
            within_10_pct=run.within_10_pct,
        )
