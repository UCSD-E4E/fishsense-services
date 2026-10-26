"""Missions and deployments (PLAN.md §4.9, Phase 2b): read by any member,
written by the tenant's admins.

* ``GET /tenants/{slug}/missions/{number}``: one mission, with its facts --
  purpose, port and what said so, place, deployment -- and its capture count;
* ``PUT .../missions/{number}/port`` and ``.../purpose``: an operator states
  them (``mission_store`` says when a port is the owner's rule and not
  theirs to change);
* ``PUT``/``DELETE .../missions/{number}/deployment[/{deployment}]``;
* ``GET``/``POST /tenants/{slug}/deployments`` and
  ``GET .../deployments/{number}``, the latter with its missions.

The list of missions stays the portal's (``portal_api``), now with the facts.
"""

from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_colour_store import mission_laser_colours
from fishsense_services_api.memberships import Membership
from fishsense_services_api.mission_facts import Port, Purpose
from fishsense_services_api.mission_store import (
    DeploymentExists,
    DeploymentNotFound,
    PortIsRuled,
    clear_mission_deployment,
    create_deployment,
    deployment_missions,
    get_deployment,
    list_deployments,
    mission_detail,
    set_mission_deployment,
    set_port,
    set_purpose,
)
from fishsense_services_api.portal_api import Mission, MissionNumber, admin_of
from fishsense_services_api.portal_store import DiveNotFound

__all__ = ["add_mission_routes"]

DeploymentNumber = Annotated[int, Path(ge=1)]


class ColourCountsView(BaseModel):
    red: int
    green: int
    #: A dot with no colour: a label without one, or an abstaining reading.
    unread: int


class LaserColoursView(BaseModel):
    """Frames by laser colour, per source; counts, never a verdict, so a
    mission whose sources disagree shows it (`laser_colour_store`)."""

    #: Labellers' choice, on live labels with a dot.
    labels: ColourCountsView
    #: The human path's laser stage, read off the dot.
    predictions: ColourCountsView
    #: The automatic chain's dot, read by the same kernel (0072).
    automatic: ColourCountsView


class MissionDetail(Mission):
    #: The Lite folder; a phone mission has none (0057).
    source_path: str | None
    notes: str | None
    port_note: str | None
    country: str | None
    region: str | None
    laser_colors: LaserColoursView


class PortStatement(BaseModel):
    #: ``unknown`` withdraws an operator's statement.
    port: Port
    #: What the operator knows it from, kept with the port.
    note: str | None = None


class PurposeStatement(BaseModel):
    purpose: Purpose | None


class _DeploymentFields(BaseModel):
    number: int
    name: str
    #: The e4edm dataset folder it came from, when it did.
    source_path: str | None
    started_on: date | None
    ended_on: date | None
    notes: str | None


class Deployment(_DeploymentFields):
    #: How many missions it holds.
    missions: int


class DeploymentDetail(_DeploymentFields):
    readme: str | None
    missions: list[Mission]


class NewDeployment(BaseModel):
    name: str = Field(min_length=1)
    started_on: date | None = None
    ended_on: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _in_order(self) -> "NewDeployment":
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            raise ValueError("ended_on is before started_on")
        return self


def add_mission_routes(app: FastAPI, *, engine: AsyncEngine, membership) -> None:
    Member = Annotated[Membership, Depends(membership)]
    Admin = Annotated[Membership, Depends(admin_of(membership))]

    async def _detail(conn, member, number) -> MissionDetail:
        found = await mission_detail(conn, member.tenant_id, number)
        mission_id = (
            await conn.execute(
                text("SELECT id FROM missions WHERE tenant_id = :t AND number = :n"),
                {"t": member.tenant_id, "n": number},
            )
        ).scalar_one()
        colours = await mission_laser_colours(conn, member.tenant_id, mission_id)
        return MissionDetail(
            **Mission.model_validate(found.summary, from_attributes=True).model_dump(),
            source_path=found.source_path,
            notes=found.notes,
            port_note=found.port_note,
            country=found.country,
            region=found.region,
            laser_colors=LaserColoursView(
                labels=ColourCountsView(**vars(colours.labels)),
                predictions=ColourCountsView(**vars(colours.predictions)),
                automatic=ColourCountsView(**vars(colours.automatic)),
            ),
        )

    @app.get(
        "/tenants/{slug}/missions/{number}",
        operation_id="get_mission",
        response_model=MissionDetail,
    )
    async def get_mission(member: Member, number: MissionNumber) -> MissionDetail:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                return await _detail(conn, member, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.put(
        "/tenants/{slug}/missions/{number}/port",
        operation_id="set_mission_port",
        response_model=MissionDetail,
    )
    async def set_mission_port(
        member: Admin, number: MissionNumber, body: PortStatement
    ) -> MissionDetail:
        """State the mission's camera port as an operator (``unknown``
        withdraws it). 409 where the owner's rules already say otherwise."""
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await set_port(
                    conn, member.tenant_id, number, body.port, note=body.note
                )
                return await _detail(conn, member, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        except PortIsRuled as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None

    @app.put(
        "/tenants/{slug}/missions/{number}/purpose",
        operation_id="set_mission_purpose",
        response_model=MissionDetail,
    )
    async def set_mission_purpose(
        member: Admin, number: MissionNumber, body: PurposeStatement
    ) -> MissionDetail:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await set_purpose(conn, member.tenant_id, number, body.purpose)
                return await _detail(conn, member, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.put(
        "/tenants/{slug}/missions/{number}/deployment/{deployment_number}",
        operation_id="set_mission_deployment",
        response_model=MissionDetail,
    )
    async def put_mission_deployment(
        member: Admin, number: MissionNumber, deployment_number: DeploymentNumber
    ) -> MissionDetail:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await set_mission_deployment(
                    conn, member.tenant_id, number, deployment_number
                )
                return await _detail(conn, member, number)
        except (DiveNotFound, DeploymentNotFound) as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.delete(
        "/tenants/{slug}/missions/{number}/deployment",
        operation_id="clear_mission_deployment",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_mission_deployment(member: Admin, number: MissionNumber) -> None:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                await clear_mission_deployment(conn, member.tenant_id, number)
        except DiveNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

    @app.get(
        "/tenants/{slug}/deployments",
        operation_id="list_deployments",
        response_model=list[Deployment],
    )
    async def get_deployments(member: Member) -> list[Deployment]:
        async with tenant_transaction(engine, member.tenant_id) as conn:
            found = await list_deployments(conn, member.tenant_id)
        return [Deployment.model_validate(d, from_attributes=True) for d in found]

    @app.get(
        "/tenants/{slug}/deployments/{number}",
        operation_id="get_deployment",
        response_model=DeploymentDetail,
    )
    async def get_one_deployment(
        member: Member, number: DeploymentNumber
    ) -> DeploymentDetail:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                found = await get_deployment(conn, member.tenant_id, number)
                missions = await deployment_missions(conn, member.tenant_id, number)
        except DeploymentNotFound as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        return DeploymentDetail(
            **_DeploymentFields.model_validate(
                found, from_attributes=True
            ).model_dump(),
            readme=found.readme,
            missions=[
                Mission.model_validate(m, from_attributes=True) for m in missions
            ],
        )

    @app.post(
        "/tenants/{slug}/deployments",
        operation_id="create_deployment",
        response_model=Deployment,
        status_code=status.HTTP_201_CREATED,
    )
    async def post_deployment(member: Admin, body: NewDeployment) -> Deployment:
        try:
            async with tenant_transaction(engine, member.tenant_id) as conn:
                created = await create_deployment(
                    conn,
                    member.tenant_id,
                    name=body.name,
                    started_on=body.started_on,
                    ended_on=body.ended_on,
                    notes=body.notes,
                )
        except DeploymentExists as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        return Deployment.model_validate(created, from_attributes=True)
