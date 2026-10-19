"""A mission's facts and the tenant's deployments, tenant-scoped (PLAN.md §4.9,
migration 0047).

* **The port** an operator states is recorded as ``operator``, with their
  note. Where the owner's rules have spoken -- a ``D``-labelled device, or the
  backfill of what was already ingested (2026-10-16) -- the port is fact, and
  a different one is refused rather than written over it: those rules are the
  only thing that makes a port known, and nothing an operator knows outranks
  them. Restating the same port changes nothing.
* **The purpose** is stated or cleared outright: nothing gates on it yet.
* **Deployments** are created by name, once per tenant, and missions join or
  leave them; a deployment of another tenant is not found.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_facts import RULED
from fishsense_services_api.portal_store import (
    DiveNotFound,
    MissionSummary,
    list_missions,
    mission_summary,
)

__all__ = [
    "DeploymentExists",
    "DeploymentNotFound",
    "DeploymentSummary",
    "MissionDetail",
    "PortIsRuled",
    "clear_mission_deployment",
    "create_deployment",
    "deployment_missions",
    "get_deployment",
    "list_deployments",
    "mission_detail",
    "set_mission_deployment",
    "set_port",
    "set_purpose",
]


class PortIsRuled(Exception):
    """The port is the owner's rule, and the request contradicts it."""


class DeploymentExists(Exception):
    """The tenant already has a deployment of that name."""


class DeploymentNotFound(LookupError):
    def __init__(self, number: int) -> None:
        super().__init__(f"deployment {number} not found")
        self.number = number


@dataclass(frozen=True)
class MissionDetail:
    summary: MissionSummary
    source_path: str
    notes: str | None
    port_note: str | None
    country: str | None
    region: str | None
    device_name: str | None
    captures: int


@dataclass(frozen=True)
class DeploymentSummary:
    number: int
    name: str
    source_path: str | None
    started_on: date | None
    ended_on: date | None
    notes: str | None
    readme: str | None
    missions: int


_DEPLOYMENTS = """
    SELECT p.number, p.name, p.source_path, p.started_on, p.ended_on, p.notes,
           p.readme, count(m.id) AS missions
    FROM deployments p
    LEFT JOIN missions m ON m.tenant_id = p.tenant_id AND m.deployment_id = p.id
    WHERE p.tenant_id = :t
"""
_DEPLOYMENTS_TAIL = " GROUP BY p.id ORDER BY p.started_on DESC NULLS LAST, p.number"


async def mission_detail(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> MissionDetail:
    row = (
        await conn.execute(
            text("""
                SELECT d.source_path, d.notes, d.port_note, d.country, d.region,
                       v.name AS device_name,
                       (SELECT count(*) FROM captures c
                        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id)
                           AS captures
                FROM missions d
                LEFT JOIN devices v ON v.tenant_id = d.tenant_id AND v.id = d.device_id
                WHERE d.tenant_id = :t AND d.number = :n
                """),
            {"t": tenant_id, "n": number},
        )
    ).one_or_none()
    if row is None:
        raise DiveNotFound("mission", number)
    return MissionDetail(
        summary=await mission_summary(conn, tenant_id, number), **row._mapping
    )


async def _locked_port(conn, tenant_id, number):
    row = (
        await conn.execute(
            text(
                "SELECT port, port_source FROM missions "
                "WHERE tenant_id = :t AND number = :n FOR UPDATE"
            ),
            {"t": tenant_id, "n": number},
        )
    ).one_or_none()
    if row is None:
        raise DiveNotFound("mission", number)
    return row


async def set_port(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    number: int,
    port: str,
    *,
    note: str | None = None,
) -> None:
    """State mission ``number``'s port as an operator, or withdraw it with
    ``unknown``. Refused (`PortIsRuled`) where the owner's rules have spoken."""
    current = await _locked_port(conn, tenant_id, number)
    if current.port_source in RULED:
        if current.port == port:
            return
        raise PortIsRuled(
            f"mission {number}'s port is {current.port} by the owner's rule "
            f"({current.port_source}); it is not changed here"
        )
    source = None if port == "unknown" else "operator"
    await conn.execute(
        text("""
            UPDATE missions SET port = :port, port_source = :source, port_note = :note
            WHERE tenant_id = :t AND number = :n
            """),
        {
            "t": tenant_id,
            "n": number,
            "port": port,
            "source": source,
            "note": note if source else None,
        },
    )


async def set_purpose(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, purpose: str | None
) -> None:
    found = (
        await conn.execute(
            text(
                "UPDATE missions SET purpose = :p WHERE tenant_id = :t AND number = :n "
                "RETURNING number"
            ),
            {"t": tenant_id, "n": number, "p": purpose},
        )
    ).scalar_one_or_none()
    if found is None:
        raise DiveNotFound("mission", number)


async def list_deployments(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[DeploymentSummary]:
    """The tenant's deployments, latest first, each with its mission count."""
    rows = await conn.execute(text(_DEPLOYMENTS + _DEPLOYMENTS_TAIL), {"t": tenant_id})
    return [DeploymentSummary(**row._mapping) for row in rows]


async def get_deployment(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> DeploymentSummary:
    row = (
        await conn.execute(
            text(_DEPLOYMENTS + " AND p.number = :n" + _DEPLOYMENTS_TAIL),
            {"t": tenant_id, "n": number},
        )
    ).one_or_none()
    if row is None:
        raise DeploymentNotFound(number)
    return DeploymentSummary(**row._mapping)


async def deployment_missions(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> list[MissionSummary]:
    return await list_missions(conn, tenant_id, deployment_number=number)


async def create_deployment(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    name: str,
    started_on: date | None = None,
    ended_on: date | None = None,
    notes: str | None = None,
) -> DeploymentSummary:
    try:
        async with conn.begin_nested():
            number = (
                await conn.execute(
                    text("""
                        INSERT INTO deployments (tenant_id, name, started_on,
                                                 ended_on, notes)
                        VALUES (:t, :name, :s, :e, :notes) RETURNING number
                        """),
                    {
                        "t": tenant_id,
                        "name": name,
                        "s": started_on,
                        "e": ended_on,
                        "notes": notes,
                    },
                )
            ).scalar_one()
    except IntegrityError as error:
        if "deployments_tenant_id_name_key" in str(error.orig):
            raise DeploymentExists(f"a deployment named {name!r} exists") from None
        raise
    return await get_deployment(conn, tenant_id, number)


async def set_mission_deployment(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int, deployment_number: int
) -> None:
    deployment = (
        await conn.execute(
            text("SELECT id FROM deployments WHERE tenant_id = :t AND number = :n"),
            {"t": tenant_id, "n": deployment_number},
        )
    ).scalar_one_or_none()
    if deployment is None:
        raise DeploymentNotFound(deployment_number)
    await _set_deployment(conn, tenant_id, number, deployment)


async def clear_mission_deployment(
    conn: AsyncConnection, tenant_id: uuid.UUID, number: int
) -> None:
    await _set_deployment(conn, tenant_id, number, None)


async def _set_deployment(conn, tenant_id, number, deployment_id) -> None:
    found = (
        await conn.execute(
            text(
                "UPDATE missions SET deployment_id = :d "
                "WHERE tenant_id = :t AND number = :n RETURNING number"
            ),
            {"t": tenant_id, "n": number, "d": deployment_id},
        )
    ).scalar_one_or_none()
    if found is None:
        raise DiveNotFound("mission", number)
