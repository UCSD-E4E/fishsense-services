"""Reviews and reviewers' skills over HTTP (0063; the owner, 2026-10-21).

Every member may review, as themselves: the reviewer is the signed-in
caller, never a field of the body. The rules (who counts, what becomes a
label) are ``review_store``'s. A tenant's admins set skills; each member reads
their own. A read-only system admin's writes are refused by the membership
dependency until they unlock the tenant (0056).
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.portal_api import admin_of
from fishsense_services_api.review_store import (
    KINDS,
    FrameNotFound,
    NotAMember,
    ReviewInput,
    ReviewRefused,
    capture_reviews,
    record_review,
    reviewer_level,
    set_skill,
)

__all__ = ["add_review_routes"]

Kind = Literal["laser", "head_tail", "species"]
Level = Literal["learning", "trusted", "expert"]


class ReviewPoint(BaseModel):
    x: float
    y: float


class ReviewBody(BaseModel):
    kind: Kind
    action: Literal["accept", "fix", "reject", "confirm"]
    automatic_head_tail_prediction_id: uuid.UUID | None = None
    reason: (
        Literal["no_fish", "fish_unmeasurable", "wrong_fish", "bad_mask", "dot_wrong"]
        | None
    ) = None
    #: The laser dot, in render pixels.
    point: ReviewPoint | None = None
    head: ReviewPoint | None = None
    tail: ReviewPoint | None = None
    #: A fix's points where they were dropped, before the snap.
    raw: dict | None = None
    render_id: uuid.UUID | None = None
    scientific_name: str | None = None
    confirms_review_id: uuid.UUID | None = None


class RecordedReviewView(BaseModel):
    review_id: uuid.UUID
    level: Level
    counts: bool
    label_id: uuid.UUID | None


class ReviewView(BaseModel):
    review_id: uuid.UUID
    reviewer_id: uuid.UUID
    kind: Kind
    action: str
    level: Level
    counts: bool
    reason: str | None
    point: ReviewPoint | None
    head: ReviewPoint | None
    tail: ReviewPoint | None
    scientific_name: str | None
    confirms_review_id: uuid.UUID | None
    created_at: datetime
    #: The caller's own review: theirs to see, not to back.
    mine: bool


class SkillBody(BaseModel):
    level: Level
    note: str | None = None


def _xy(p: ReviewPoint | None):
    return None if p is None else (p.x, p.y)


def _pt(p):
    return None if p is None else ReviewPoint(x=p[0], y=p[1])


def add_review_routes(app: FastAPI, *, engine: AsyncEngine, membership) -> None:
    Member = Annotated[Membership, Depends(membership)]
    Admin = Annotated[Membership, Depends(admin_of(membership))]

    @app.get(
        "/tenants/{slug}/captures/{capture_id}/reviews",
        operation_id="list_capture_reviews",
        response_model=list[ReviewView],
    )
    async def list_reviews(capture_id: uuid.UUID, member: Member) -> list[ReviewView]:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await capture_reviews(conn, member.tenant_id, capture_id)
        return [
            ReviewView(
                review_id=r.review_id, reviewer_id=r.reviewer_id, kind=r.kind,
                action=r.action, level=r.level, counts=r.counts, reason=r.reason,
                point=_pt(r.point), head=_pt(r.head), tail=_pt(r.tail),
                scientific_name=r.scientific_name,
                confirms_review_id=r.confirms_review_id, created_at=r.created_at,
                mine=r.reviewer_id == member.user_id,
            )
            for r in found
        ]  # fmt: skip

    @app.post(
        "/tenants/{slug}/captures/{capture_id}/reviews",
        operation_id="record_review",
        response_model=RecordedReviewView,
        status_code=status.HTTP_201_CREATED,
    )
    async def post_review(
        capture_id: uuid.UUID, body: ReviewBody, member: Member
    ) -> RecordedReviewView:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                recorded = await record_review(
                    conn,
                    member.tenant_id,
                    ReviewInput(
                        capture_id=capture_id,
                        reviewer_id=member.user_id,
                        kind=body.kind,
                        action=body.action,
                        automatic_head_tail_prediction_id=body.automatic_head_tail_prediction_id,
                        reason=body.reason,
                        point=_xy(body.point),
                        head=_xy(body.head),
                        tail=_xy(body.tail),
                        raw=body.raw,
                        render_id=body.render_id,
                        scientific_name=body.scientific_name,
                        confirms_review_id=body.confirms_review_id,
                    ),
                )
        except FrameNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        except ReviewRefused as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
            ) from None
        return RecordedReviewView(**vars(recorded))

    @app.get(
        "/tenants/{slug}/me/skills",
        operation_id="get_my_skills",
        response_model=dict[str, Level],
    )
    async def my_skills(member: Member) -> dict[str, str]:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            return {
                kind: await reviewer_level(conn, member.tenant_id, member.user_id, kind)
                for kind in KINDS
            }

    @app.put(
        "/tenants/{slug}/members/{user_id}/skills/{kind}",
        operation_id="set_member_skill",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def put_skill(
        user_id: uuid.UUID, kind: Kind, body: SkillBody, admin: Admin
    ) -> Response:
        """An admin sets a member's level in one kind; it is recorded as theirs."""
        try:
            async with tenant_transaction(engine, admin.tenant_id) as conn:
                await set_skill(conn, admin.tenant_id, user_id, kind, body.level,
                                set_by=admin.user_id, note=body.note)  # fmt: skip
        except NotAMember as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)
