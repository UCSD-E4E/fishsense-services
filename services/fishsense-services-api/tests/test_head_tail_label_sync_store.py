"""The database side of the head/tail label sync, tenant-scoped.

Ported from fishsense-lite@77e8f8e5: services/fishsense-api/tests/
test_label_studio_project_ids_superseded.py (the head/tail endpoint) and the
row semantics of services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
sync_headtail_labels_for_label_studio_project_activity.py
(`__update_headtail_label`). v1's rules, kept:

* the projects are the distinct projects of live (not superseded) head/tail
  labels;
* a label is found by its Label Studio task **among live rows** (v1 looked it
  up with `superseded == False`); a task with none is skipped;
* the points are written only when *both* Snout and Fork are present;
  otherwise the last ones stay;
* the annotator is best effort: an unresolvable one keeps the last.

v2 changes, each pinned here (the laser sync's, applied to head/tail):

* per tenant;
* **the sync writes only the columns it owns.** v1 PUT the whole row back, so
  a `needs_reprocess` raised in between was silently cleared; here the update
  names its columns, and `needs_reprocess` and `superseded` are not among them.
"""

import itertools
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.label_sync_store import (
    HeadTailSync,
    LabelSyncCatalog,
    apply_head_tail_sync,
    label_studio_projects,
)

T0 = datetime(2026, 4, 10, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"
_PROJECTS = itertools.count(500)

SYNCED = HeadTailSync(
    completed=True,
    head_x=10.0,
    head_y=40.0,
    tail_x=30.0,
    tail_y=80.0,
    ls_labeler_id=141592,
    ls_updated_at=T0,
    ls_payload={"id": 7, "annotations": [{"result": []}]},
)


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _head_tail(owner_engine, tenant, *, project=None, task, **extra):
    columns = {"completed": False, "superseded": False, "needs_reprocess": False,
               **extra}  # fmt: skip
    async with owner_engine.begin() as conn:
        capture = (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, source_path, captured_at, "
                    "checksum) VALUES (:t, :p, now(), :c) RETURNING id"
                ),
                {"t": tenant, "p": f"/{uuid.uuid4()}.ORF", "c": uuid.uuid4().hex},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO head_tail_labels (tenant_id, capture_id, source, "
                "ls_project_id, ls_task_id, completed, superseded, needs_reprocess) "
                "VALUES (:t, :c, 'human', :p, :k, :completed, :superseded, "
                ":needs_reprocess)"
            ),
            {"t": tenant, "c": capture, "p": project or next(_PROJECTS), "k": task,
             **columns},
        )  # fmt: skip


async def _label(owner_engine, task):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM head_tail_labels WHERE ls_task_id = :k"),
                {"k": task},
            )
        ).one()


async def _apply(app_engine, tenant, task, sync=SYNCED):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await apply_head_tail_sync(conn, tenant, task, sync)


async def test_projects_are_the_distinct_live_head_tail_label_projects(
    owner_engine, app_engine
):
    """A project whose labels were all dead-lettered is not live labeling
    work, and must drop off the sync enumeration (and the landing page)."""
    lab = await _tenant(owner_engine)
    for project, task, superseded in ((71, 1, False), (71, 2, False), (76, 3, True)):
        await _head_tail(owner_engine, lab, project=project, task=task,
                         superseded=superseded)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await label_studio_projects(conn, lab, "head_tail") == [71]
        assert await label_studio_projects(conn, lab, "laser") == []


async def test_a_task_updates_its_label(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    await _head_tail(owner_engine, lab, task=7)

    assert await _apply(app_engine, lab, 7) is True

    label = await _label(owner_engine, 7)
    assert label.completed is True
    assert (label.head_x, label.head_y, label.tail_x, label.tail_y) == (
        10.0,
        40.0,
        30.0,
        80.0,
    )
    assert (label.ls_labeler_id, label.ls_updated_at) == (141592, T0)
    assert label.ls_payload == {"id": 7, "annotations": [{"result": []}]}


async def test_a_task_with_no_label_is_skipped(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    assert await _apply(app_engine, lab, 404) is False


async def test_a_superseded_label_is_not_found_by_its_task(owner_engine, app_engine):
    """v1 looked the label up with `superseded == False`: a dead-lettered row
    is not live work, and its task's annotation must not revive it."""
    lab = await _tenant(owner_engine)
    await _head_tail(owner_engine, lab, task=7, superseded=True)

    assert await _apply(app_engine, lab, 7) is False
    assert (await _label(owner_engine, 7)).completed is False


async def test_another_tenants_task_is_never_updated(owner_engine, app_engine):
    lab, partner = await _tenant(owner_engine), await _tenant(owner_engine, "partner")
    await _head_tail(owner_engine, partner, task=7)

    assert await _apply(app_engine, lab, 7) is False
    assert (await _label(owner_engine, 7)).completed is False


async def test_the_sync_never_clears_a_reprocess_flag(owner_engine, app_engine):
    """v2: the flag is the cohort's, not Label Studio's, so the sync's update
    has no column list that could carry it (v1's whole-row PUT cleared it)."""
    lab = await _tenant(owner_engine)
    await _head_tail(owner_engine, lab, task=7, needs_reprocess=True)

    await _apply(app_engine, lab, 7)

    assert (await _label(owner_engine, 7)).needs_reprocess is True


async def test_points_move_only_when_both_keypoints_are_present(
    owner_engine, app_engine
):
    """v1 wrote the four coordinates only when Snout and Fork were both there;
    a half-placed annotation keeps the last pair."""
    lab = await _tenant(owner_engine)
    await _head_tail(owner_engine, lab, task=7)
    await _apply(app_engine, lab, 7)

    half = HeadTailSync(
        completed=False, head_x=None, head_y=None, tail_x=None, tail_y=None,
        ls_labeler_id=None, ls_updated_at=T0 + timedelta(hours=1), ls_payload={},
    )  # fmt: skip
    await _apply(app_engine, lab, 7, half)

    label = await _label(owner_engine, 7)
    assert label.completed is False
    assert (label.head_x, label.tail_y) == (10.0, 80.0)
    assert label.ls_labeler_id == 141592, "an unresolvable annotator keeps the last"
    assert label.ls_updated_at == T0 + timedelta(hours=1)


async def test_the_catalog_applies_within_a_tenant(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    await _head_tail(owner_engine, lab, project=71, task=7)
    catalog = LabelSyncCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.label_studio_projects(lab, "head_tail") == [71]
    assert await catalog.apply_head_tail_sync(lab, 7, SYNCED) is True
