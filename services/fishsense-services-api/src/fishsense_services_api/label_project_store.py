"""The database side of creating a Label Studio project, tenant-scoped.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/populate_utils.py: the dive lookup in
`build_per_dive_title` (v1 fetched the dive's name and id through the API).
v1 kept no record of which project held which dive's labels -- every create
searched Label Studio for the title `{dive.name} #{dive_id} - {suffix}`.

v2 changes:

* per tenant;
* **every project created or found is recorded** in label_studio_projects
  (migration 0020), and a dive's project is looked up here first, keyed on the
  dive, so renaming a dive no longer strands its project (v1's next populate
  created a second one and split the dive's labels across two);
* titles embed the dive's `number`, v1's dive id for a migrated dive, so the
  title search still finds every v1 project;
* **only a titled record is trusted.** migrate-v1 records each project v1's
  labels point at, untitled, against the dive holding most of its labels --
  the grandfathered shared projects included. The title search heals those
  records when it finds the dive's own project.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "KINDS",
    "DiveForTitle",
    "LabelProjectCatalog",
    "RecordedProject",
    "dive_for_title",
    "record_project",
    "recorded_project",
]

#: Migration 0020's kinds: v1's four per-dive labeling projects and the
#: checkerboard lattice study (one project for every dive).
KINDS = ("laser", "head_tail", "slate", "species", "checkerboard_lattice")


@dataclass(frozen=True)
class DiveForTitle:
    """What a per-dive project title is built from."""

    #: v1's dive id for a migrated dive (migration 0019).
    number: int
    name: str | None


@dataclass(frozen=True)
class RecordedProject:
    ls_project_id: int
    title: str


async def dive_for_title(
    conn: AsyncConnection, tenant_id: uuid.UUID, dive_id: uuid.UUID
) -> DiveForTitle | None:
    """The tenant's dive's number and name; None if it has no such dive."""
    row = (
        await conn.execute(
            text("SELECT number, name FROM dives WHERE tenant_id = :t AND id = :d"),
            {"t": tenant_id, "d": dive_id},
        )
    ).one_or_none()
    return None if row is None else DiveForTitle(number=row.number, name=row.name)


async def recorded_project(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    dive_id: uuid.UUID | None,
    title: str,
) -> RecordedProject | None:
    """The newest titled project recorded for the dive and kind.

    Keyed on the dive, not the title, so a renamed dive keeps its project. A
    project of no dive (the lattice study) is keyed on its fixed `title`.
    Newest first: a project recreated after its predecessor was deleted in
    Label Studio is the one to use.
    """
    if dive_id is None:
        match, params = "dive_id IS NULL AND title = :title", {"title": title}
    else:
        match, params = "dive_id = :dive", {"dive": dive_id}
    row = (
        await conn.execute(
            text(f"""
                SELECT ls_project_id, title FROM label_studio_projects
                WHERE tenant_id = :tenant AND kind = :kind
                  AND title IS NOT NULL AND {match}
                ORDER BY created_at DESC, ls_project_id DESC
                LIMIT 1
                """),
            {"tenant": tenant_id, "kind": kind, **params},
        )
    ).one_or_none()
    return None if row is None else RecordedProject(row.ls_project_id, row.title)


async def record_project(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    kind: str,
    *,
    dive_id: uuid.UUID | None,
    ls_project_id: int,
    title: str,
) -> None:
    """Record that `ls_project_id` is the dive's project of `kind`.

    A project already recorded (migrate-v1's untitled row) is healed in place:
    its title filled in and its dive set to the one the title names.
    """
    await conn.execute(
        text("""
            INSERT INTO label_studio_projects
                (tenant_id, dive_id, kind, ls_project_id, title)
            VALUES (:tenant, :dive, :kind, :project, :title)
            ON CONFLICT (tenant_id, kind, ls_project_id) DO UPDATE SET
                dive_id = excluded.dive_id, title = excluded.title
            WHERE (label_studio_projects.dive_id, label_studio_projects.title)
                IS DISTINCT FROM (excluded.dive_id, excluded.title)
            """),
        {"tenant": tenant_id, "dive": dive_id, "kind": kind,
         "project": ls_project_id, "title": title},
    )  # fmt: skip


class LabelProjectCatalog(ServicePrincipal):
    """Label Studio project records, as the orchestrator's service principal."""

    async def dive_for_title(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> DiveForTitle | None:
        async with self._tenant(tenant_id) as conn:
            return await dive_for_title(conn, tenant_id, dive_id)

    async def recorded_project(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        *,
        dive_id: uuid.UUID | None,
        title: str,
    ) -> RecordedProject | None:
        async with self._tenant(tenant_id) as conn:
            return await recorded_project(
                conn, tenant_id, kind, dive_id=dive_id, title=title
            )

    async def record_project(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        *,
        dive_id: uuid.UUID | None,
        ls_project_id: int,
        title: str,
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_project(
                conn,
                tenant_id,
                kind,
                dive_id=dive_id,
                ls_project_id=ls_project_id,
                title=title,
            )
