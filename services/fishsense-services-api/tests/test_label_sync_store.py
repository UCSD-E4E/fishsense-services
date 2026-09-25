"""The database side of the Label Studio label sync, tenant-scoped.

Ported from fishsense-lite@a8b2c3bc: the API's laser-label project ids
(`get_laser_label_studio_project_ids`), its sync-cursor endpoints, and the laser
label PUT the sync used. v1's semantics, kept:

* the projects are the distinct Label Studio projects of live (not superseded)
  laser labels;
* a label is found by its Label Studio task; a task with no label is skipped;
* the cursor is per (kind, project).

v2 changes, each pinned here:

* everything is per tenant;
* **the sync writes only the columns it owns.** v1 read a label, changed a few
  fields and PUT the whole row back, so a `needs_reprocess` flag raised between
  that read and that write was silently cleared (v1 documents it and leaves
  it). v2's update names its columns, and never touches `needs_reprocess` or
  `superseded`;
* **the cursor never moves backwards.** Two overlapping runs (the schedule
  allows overlap, as v1's did) can finish out of order; the later write must
  not rewind the earlier one's progress.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import create_dive, register_capture
from fishsense_services_api.label_sync_store import (
    LabelSyncCatalog,
    LaserSync,
    advance_sync_cursor,
    apply_laser_sync,
    label_studio_projects,
    sync_cursor,
)

T0 = datetime(2026, 4, 10, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _capture(app_engine, tenant) -> uuid.UUID:
    async with tenant_transaction(app_engine, tenant) as conn:
        dive = await create_dive(
            conn, tenant, source_path=f"d-{uuid.uuid4()}", name="d", dived_at=T0
        )
        return (
            await register_capture(
                conn, tenant, dive_id=dive, device_id=None,
                source_path=f"{dive}/P.ORF", captured_at=T0,
                checksum=uuid.uuid4().hex,
            )  # fmt: skip
        ).capture_id


async def _laser(owner_engine, tenant, capture, *, project, task, **extra):
    columns = {"completed": False, "superseded": False, "needs_reprocess": False,
               **extra}  # fmt: skip
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_labels (tenant_id, capture_id, source, "
                "ls_project_id, ls_task_id, completed, superseded, needs_reprocess) "
                "VALUES (:t, :c, 'human', :p, :k, :completed, :superseded, "
                ":needs_reprocess)"
            ),
            {"t": tenant, "c": capture, "p": project, "k": task, **columns},
        )


async def _label(owner_engine, task):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM laser_labels WHERE ls_task_id = :k"), {"k": task}
            )
        ).one()


SYNCED = LaserSync(
    completed=True,
    x=100.0,
    y=160.0,
    label="laser",
    ls_labeler_id=141592,
    ls_updated_at=T0,
    ls_payload={"id": 7, "annotations": []},
)


# -- the projects --------------------------------------------------------------------


async def test_projects_are_the_distinct_live_laser_label_projects(
    owner_engine, app_engine
):
    """Dead-lettered rows aren't live labeling work -- v1 mirrored every other
    laser read, so a superseded-only project drops off the sync enumeration."""
    lab = await _tenant(owner_engine)
    for project, task, superseded in ((43, 1, False), (43, 2, False), (44, 3, True)):
        capture = await _capture(app_engine, lab)
        await _laser(owner_engine, lab, capture, project=project, task=task,
                     superseded=superseded)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await label_studio_projects(conn, lab, "laser") == [43]


async def test_another_tenants_projects_are_not_listed(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    await _laser(owner_engine, partner, await _capture(app_engine, partner),
                 project=99, task=1)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await label_studio_projects(conn, lab, "laser") == []


# -- applying a task -----------------------------------------------------------------


async def test_a_task_updates_its_label(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    await _laser(owner_engine, lab, await _capture(app_engine, lab), project=43, task=7)

    async with tenant_transaction(app_engine, lab) as conn:
        found = await apply_laser_sync(conn, lab, 7, SYNCED)

    label = await _label(owner_engine, 7)
    assert found is True
    assert (label.completed, label.x, label.y, label.label) == (True, 100.0, 160.0,
                                                                 "laser")  # fmt: skip
    assert (label.ls_labeler_id, label.ls_updated_at) == (141592, T0)
    assert label.ls_payload == {"id": 7, "annotations": []}


async def test_a_task_with_no_label_is_skipped(owner_engine, app_engine):
    """v1's `test_skips_tasks_with_no_existing_label`: labels are created when
    a project is populated; sync only updates them."""
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await apply_laser_sync(conn, lab, 404, SYNCED) is False


async def test_another_tenants_task_is_never_updated(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    await _laser(owner_engine, partner, await _capture(app_engine, partner),
                 project=99, task=7)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await apply_laser_sync(conn, lab, 7, SYNCED) is False
    assert (await _label(owner_engine, 7)).completed is False


async def test_the_sync_never_clears_a_reprocess_flag_or_a_supersession(
    owner_engine, app_engine
):
    """v1's documented race, closed: the flag belongs to the cohort, not to
    Label Studio, so the sync has no column list that could carry it."""
    lab = await _tenant(owner_engine)
    await _laser(owner_engine, lab, await _capture(app_engine, lab), project=43,
                 task=7, needs_reprocess=True, superseded=True)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        await apply_laser_sync(conn, lab, 7, SYNCED)

    label = await _label(owner_engine, 7)
    assert (label.needs_reprocess, label.superseded) == (True, True)


async def test_a_task_without_a_keypoint_keeps_the_last_one(owner_engine, app_engine):
    """v1 set x/y/label only when the annotation carried a keypoint, and left
    them otherwise. Unlabeling marks the label incomplete, which is what every
    cohort's 'valid laser' test reads."""
    lab = await _tenant(owner_engine)
    await _laser(owner_engine, lab, await _capture(app_engine, lab), project=43, task=7)
    async with tenant_transaction(app_engine, lab) as conn:
        await apply_laser_sync(conn, lab, 7, SYNCED)
        await apply_laser_sync(
            conn, lab, 7,
            LaserSync(completed=False, x=None, y=None, label=None,
                      ls_labeler_id=None, ls_updated_at=T0 + timedelta(hours=1),
                      ls_payload={}),
        )  # fmt: skip

    label = await _label(owner_engine, 7)
    assert (label.completed, label.x, label.y, label.label) == (False, 100.0, 160.0,
                                                                 "laser")  # fmt: skip
    assert label.ls_labeler_id == 141592, "an unresolvable annotator keeps the last"


# -- the cursor ----------------------------------------------------------------------


async def test_a_cursor_starts_empty_and_advances(owner_engine, app_engine):
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await sync_cursor(conn, lab, "laser", 42) is None
        await advance_sync_cursor(conn, lab, "laser", 42, T0)
        await advance_sync_cursor(conn, lab, "laser", 42, T0 + timedelta(days=2))
        assert await sync_cursor(conn, lab, "laser", 42) == T0 + timedelta(days=2)


async def test_a_cursor_never_moves_backwards(owner_engine, app_engine):
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        await advance_sync_cursor(conn, lab, "laser", 42, T0 + timedelta(days=2))
        await advance_sync_cursor(conn, lab, "laser", 42, T0)
        assert await sync_cursor(conn, lab, "laser", 42) == T0 + timedelta(days=2)


async def test_cursors_are_per_kind_and_project(owner_engine, app_engine):
    """v1's `test_kind_is_forwarded_to_cursor_calls`: laser and head/tail keep
    separate cursors per project."""
    lab = await _tenant(owner_engine)

    async with tenant_transaction(app_engine, lab) as conn:
        await advance_sync_cursor(conn, lab, "laser", 42, T0)
        assert await sync_cursor(conn, lab, "head_tail", 42) is None
        assert await sync_cursor(conn, lab, "laser", 43) is None


# -- the catalog, as the orchestrator's principal ------------------------------------


async def test_the_catalog_syncs_within_a_tenant(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    await _laser(owner_engine, lab, await _capture(app_engine, lab), project=43, task=7)
    catalog = LabelSyncCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.label_studio_projects(lab, "laser") == [43]
    assert await catalog.apply_laser_sync(lab, 7, SYNCED) is True
    await catalog.advance_sync_cursor(lab, "laser", 43, T0)
    assert await catalog.sync_cursor(lab, "laser", 43) == T0
