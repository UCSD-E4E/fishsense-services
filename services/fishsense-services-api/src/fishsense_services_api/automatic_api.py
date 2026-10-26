"""The automatic track over HTTP, for review (the owner, 2026-10-21: "use
this app to review the auto process").

Read-only, for any member of the tenant; the web shows it as automatic and
unvalidated. The rules are in ``automatic_review_store``. Here: a frame's
render and mask go out as presigned links when they are in the tenant's
current bucket, and a frame or mission the caller can't see, or the run never
reached, is 404. Lengths are listed a page at a time, for the tenant or one
mission, each with its reviews and the human length on its frame
(``automatic_lengths_store``; the owner, 2026-10-24).
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.automatic_lengths_store import (
    AutomaticLengthPage,
    automatic_lengths,
    parse_cursor,
)
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
    #: The current prediction: what a review of it names.
    prediction_id: uuid.UUID
    #: ``predicted``, or why the run abstained.
    status: str
    predicted_at: datetime
    #: Full-frame pixels of ``render_url``'s image.
    laser: PixelPoint | None
    laser_confidence: float | None
    #: ``red`` or ``green``, as the laser kernel read the dot (0072); None
    #: when it gave no reading, and on every row from before.
    laser_color: str | None
    laser_color_margin: float | None
    #: ``dot`` (read with the dot), ``render`` (backfilled off the render,
    #: 0076), or None when there is no reading.
    laser_color_source: str | None
    head: PixelPoint | None
    tail: PixelPoint | None
    mask_bbox: list[int] | None
    sam_score: float | None
    slate_probability: float | None
    predictor_version: int
    checkpoint: str | None
    core_version: str | None
    render_url: str | None
    #: Named by a review of these points (0063).
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


class KindReviewView(BaseModel):
    #: The latest review that counts: it decided the frame's label of this
    #: kind. None while none counts.
    action: str | None
    reason: str | None
    #: A newer review waits for a Trusted reviewer, or a second learner.
    pending: bool


class AutomaticLengthView(BaseModel):
    mission_number: int
    capture_id: uuid.UUID
    capture_number: int
    captured_at: datetime | None
    length_m: float
    depth_m: float | None
    #: Always ``label_free`` (0064): the automatic stack alone.
    calibration_source: str
    laser_confidence: float | None
    #: The automatic dot's colour (0072); None when unread or from before.
    laser_color: str | None
    laser_color_margin: float | None
    #: ``dot``, ``render`` (backfilled off the render, 0076), or None.
    laser_color_source: str | None
    sam_score: float | None
    #: The predicted choice, as Label Studio spells it.
    species: str | None
    scientific_name: str | None
    species_probability: float | None
    species_margin: float | None
    #: The frame's human length, when it has exactly one laser-measured fish.
    human_length_m: float | None
    #: The mission has a current human length somewhere.
    mission_human_measured: bool
    #: By kind (``laser``, ``head_tail``, ``species``); unreviewed kinds absent.
    reviews: dict[str, KindReviewView]


class AutomaticLengthPageView(BaseModel):
    lengths: list[AutomaticLengthView]
    #: Pass as ``after`` for the next page; None at the end. A page can hold
    #: fewer than ``limit`` lengths and still have a next.
    next: str | None


def _page(page: AutomaticLengthPage) -> AutomaticLengthPageView:
    return AutomaticLengthPageView(
        lengths=[
            AutomaticLengthView(
                **{**vars(r), "reviews": {k: vars(v) for k, v in r.reviews.items()}}
            )
            for r in page.lengths
        ],
        next=page.next,
    )


def _cursor(after: str | None) -> tuple[int, int] | None:
    try:
        return parse_cursor(after)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


Limit = Annotated[int, Query(ge=1, le=500)]
After = Annotated[str | None, Query(max_length=40)]


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
            prediction_id=found.prediction_id,
            status=found.status,
            predicted_at=found.created_at,
            laser=_point(found.laser),
            laser_confidence=found.laser_confidence,
            laser_color=found.laser_color,
            laser_color_margin=found.laser_color_margin,
            laser_color_source=found.laser_color_source,
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
        "/tenants/{slug}/captures/{capture_id}/automatic/mask",
        operation_id="get_automatic_mask",
        response_class=Response,
        responses={200: {"content": {"image/png": {}}}},
    )
    async def get_mask(slug: str, capture_id: uuid.UUID, member: Member) -> Response:
        """The current prediction's mask, as its PNG bytes (2026-10-22). A fix's
        head and tail snap to it in the browser, as the app's do, and a page can
        read an image's pixels only from its own origin: the web passes these
        on. Read through the store's internal endpoint."""
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await automatic_frame(conn, member.tenant_id, capture_id)
        if found is None or found.mask_key is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no mask for this frame")
        try:
            objects = stores.for_tenant(slug)
        except NoTenantStore:
            objects = None
        data = (
            await run_in_threadpool(objects.get, found.mask_key)
            if objects is not None and objects.bucket == found.mask_bucket
            else None
        )
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no mask for this frame")
        return Response(
            data,
            media_type="image/png",
            # A mask is written once per prediction; private: a member's.
            headers={"cache-control": "private, max-age=3600"},
        )

    @app.get(
        "/tenants/{slug}/automatic/lengths",
        operation_id="list_automatic_lengths",
        response_model=AutomaticLengthPageView,
    )
    async def list_lengths(
        member: Member,
        species: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        after: After = None,
        limit: Limit = 200,
    ) -> AutomaticLengthPageView:
        """The tenant's current automatic lengths, by mission and capture
        number; ``species`` keeps one, by scientific name. Unvalidated."""
        cursor = _cursor(after)
        async with tenant_transaction(engine, member.tenant_id) as conn:
            page = await automatic_lengths(
                conn, member.tenant_id, species=species, after=cursor, limit=limit
            )
        return _page(page)

    @app.get(
        "/tenants/{slug}/missions/{number}/automatic/lengths",
        operation_id="list_mission_automatic_lengths",
        response_model=AutomaticLengthPageView,
    )
    async def list_mission_lengths(
        number: Annotated[int, Path(ge=0)],
        member: Member,
        after: After = None,
        limit: Limit = 200,
    ) -> AutomaticLengthPageView:
        """One mission's current automatic lengths, by capture number."""
        cursor = _cursor(after)
        async with tenant_transaction(engine, member.tenant_id) as conn:
            mission_id = await _mission_id(conn, member.tenant_id, number)
            page = await automatic_lengths(
                conn, member.tenant_id, mission_id=mission_id, after=cursor, limit=limit
            )
        return _page(page)

    @app.get(
        "/tenants/{slug}/missions/{number}/automatic",
        operation_id="get_mission_automatic",
        response_model=MissionAutomaticView,
    )
    async def get_mission(
        number: Annotated[int, Path(ge=0)], member: Member
    ) -> MissionAutomaticView:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            mission_id = await _mission_id(conn, member.tenant_id, number)
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


async def _mission_id(conn, tenant_id: uuid.UUID, number: int) -> uuid.UUID:
    mission_id = (
        await conn.execute(
            text("SELECT id FROM missions WHERE tenant_id = :t AND number = :n"),
            {"t": tenant_id, "n": number},
        )
    ).scalar_one_or_none()
    if mission_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such mission")
    return mission_id
