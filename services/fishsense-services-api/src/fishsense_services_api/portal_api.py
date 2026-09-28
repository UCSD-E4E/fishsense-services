"""The web portal's routes: tenant-scoped, and admin-only where they write.

Ported from fishsense-lite@77e8f8e5, the routes apps/fishsense-lite-web
called on v1's fishsense-api:

* `GET /api/v1/labels/{laser,headtail,species,dive-slate}/label-studio-project-ids`
  -> `GET /tenants/{slug}/label-studio-projects?kind=`;
* `GET /api/v1/dives/` -> `GET /tenants/{slug}/dives`;
* `PUT /api/v1/dives/{id}/calibration-source/{source_id}` and
  `DELETE /api/v1/dives/{id}/calibration-source/` -> the same under the
  tenant, by dive `number`;
* `PUT /api/v1/dives/{id}/calibration-target/{calibration_target_id}`,
  `DELETE /api/v1/dives/{id}/calibration-target/` and
  `DELETE /api/v1/dives/{id}/calibration-refused/` -> `.../calibration-target`
  (by the target's `number`) and `.../calibration-refusal` under the tenant:
  the levers a calibration refusal's recorded remedy names (v1's web had no
  page for them; only the SDK's `DiveClient` called them).

v1's API enforced nothing (the web called it as one Basic-auth service
account, and gated only its own pages on an Authentik group). Here every route
needs a member's token, the writes need the tenant's `admin` role, and
`GET /tenants/{slug}/membership` tells the web what the caller may do.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.memberships import Membership
from fishsense_services_api.portal_store import (
    CalibrationTargetNotFound,
    DiveNotFound,
    DiveSummary,
    GateNotApplicable,
    LabelKind,
    SelfLink,
    clear_calibration_refusal,
    clear_calibration_source,
    clear_calibration_target,
    label_studio_project_ids,
    list_dives,
    set_calibration_source,
    set_calibration_target,
    set_needs_reprocess,
)


class MyMembership(BaseModel):
    role: str
    is_admin: bool


class Flagged(BaseModel):
    flagged: int


class Cleared(BaseModel):
    cleared: int


class Dive(BaseModel):
    number: int
    name: str | None
    dived_at: datetime
    priority: Literal["low", "high", "none"]
    slate_template_number: int | None
    calibration_target_number: int | None
    calibration_source_number: int | None


# A dive's number: v1's id for a migrated dive. Non-negative, as v1's web
# checked (`safeId`) before putting one in a URL.
DiveNumber = Annotated[int, Path(ge=0)]


def add_portal_routes(app: FastAPI, *, engine: AsyncEngine, membership) -> None:
    """Register the portal's routes. ``membership`` is the app's dependency that
    authenticates the caller and resolves their membership in ``{slug}``."""

    Member = Annotated[Membership, Depends(membership)]

    def admin(member: Member) -> Membership:
        # Membership first (a non-member gets the same 404 as everywhere),
        # then the role -- before any dive is looked up, so a non-admin
        # learns nothing about which dives exist.
        if not member.is_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "this needs the tenant's admin role"
            )
        return member

    Admin = Annotated[Membership, Depends(admin)]

    @app.get(
        "/tenants/{slug}/membership",
        operation_id="get_my_membership",
        response_model=MyMembership,
    )
    async def get_my_membership(member: Member) -> MyMembership:
        return MyMembership(role=member.role, is_admin=member.is_admin)

    @app.get(
        "/tenants/{slug}/label-studio-projects",
        operation_id="list_label_studio_project_ids",
        response_model=list[int],
    )
    async def list_label_studio_project_ids(
        member: Member,
        kind: LabelKind,
        incomplete: bool = False,
        gated: Annotated[
            bool | None,
            Query(description="laser only: the auto-accept gate is done here"),
        ] = None,
    ) -> list[int]:
        """Label Studio project ids with live labels of ``kind``."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                return await label_studio_project_ids(
                    conn, member.tenant_id, kind, incomplete=incomplete, gated=gated
                )
        except GateNotApplicable as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
            ) from None

    @app.get(
        "/tenants/{slug}/dives", operation_id="list_dives", response_model=list[Dive]
    )
    async def get_dives(member: Member) -> list[Dive]:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            return [_dive(d) for d in await list_dives(conn, member.tenant_id)]

    @app.put(
        "/tenants/{slug}/dives/{number}/calibration-source/{source_number}",
        operation_id="set_dive_calibration_source",
        response_model=Dive,
    )
    async def set_dive_calibration_source(
        member: Admin, number: DiveNumber, source_number: DiveNumber
    ) -> Dive:
        """Dive ``number`` borrows dive ``source_number``'s laser calibration.

        400 on a self-link; 404 if either dive is missing from the tenant.
        """
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                summary = await set_calibration_source(
                    conn, member.tenant_id, number, source_number
                )
        except SelfLink as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return _dive(summary)

    @app.delete(
        "/tenants/{slug}/dives/{number}/calibration-source",
        operation_id="clear_dive_calibration_source",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def clear_dive_calibration_source(member: Admin, number: DiveNumber) -> None:
        """Unlink dive ``number`` from any borrowed calibration (idempotent)."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await clear_calibration_source(conn, member.tenant_id, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.put(
        "/tenants/{slug}/dives/{number}/calibration-target/{target_number}",
        operation_id="set_dive_calibration_target",
        response_model=Dive,
    )
    async def set_dive_calibration_target(
        member: Admin, number: DiveNumber, target_number: DiveNumber
    ) -> Dive:
        """Dive ``number`` was shot against calibration target
        ``target_number``: it enters the checkerboard cohort, and a standing
        calibration refusal expires.

        404 if the dive is missing from the tenant or no target has that
        number.
        """
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                summary = await set_calibration_target(
                    conn, member.tenant_id, number, target_number
                )
        except (DiveNotFound, CalibrationTargetNotFound) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return _dive(summary)

    @app.delete(
        "/tenants/{slug}/dives/{number}/calibration-target",
        operation_id="clear_dive_calibration_target",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def clear_dive_calibration_target(member: Admin, number: DiveNumber) -> None:
        """Unlink dive ``number`` from any calibration target (idempotent): it
        leaves the checkerboard cohort. A standing refusal is left as it is."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await clear_calibration_target(conn, member.tenant_id, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.delete(
        "/tenants/{slug}/dives/{number}/calibration-refusal",
        operation_id="clear_dive_calibration_refusal",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def clear_dive_calibration_refusal(
        member: Admin,
        number: DiveNumber,
        reason: Annotated[
            str | None, Query(description="why, kept with the clear")
        ] = None,
    ) -> None:
        """Clear dive ``number``'s standing calibration refusal, so it is
        fitted again (idempotent): for a change its labels don't show."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await clear_calibration_refusal(
                    conn, member.tenant_id, number, reason=reason
                )
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.put(
        "/tenants/{slug}/dives/{number}/labels/{kind}/needs-reprocess",
        operation_id="raise_needs_reprocess",
        response_model=Flagged,
    )
    async def raise_needs_reprocess(
        member: Admin,
        number: DiveNumber,
        kind: LabelKind,
        only_incomplete: bool = True,
    ) -> Flagged:
        """Redraw dive ``number``'s ``kind`` frames: the dive re-enters that
        kind's preprocessing, and the JPEGs are redrawn where they are.
        Incomplete labels only unless ``only_incomplete=false``."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                flagged = await set_needs_reprocess(
                    conn, member.tenant_id, number, kind,
                    raised=True, only_incomplete=only_incomplete,
                )  # fmt: skip
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return Flagged(flagged=flagged)

    @app.delete(
        "/tenants/{slug}/dives/{number}/labels/{kind}/needs-reprocess",
        operation_id="clear_needs_reprocess",
        response_model=Cleared,
    )
    async def clear_needs_reprocess(
        member: Admin, number: DiveNumber, kind: LabelKind
    ) -> Cleared:
        """Withdraw a redraw of dive ``number``'s ``kind`` frames (idempotent:
        0 when none was asked for)."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                cleared = await set_needs_reprocess(
                    conn, member.tenant_id, number, kind, raised=False
                )
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return Cleared(cleared=cleared)


def _dive(summary: DiveSummary) -> Dive:
    return Dive.model_validate(summary, from_attributes=True)
