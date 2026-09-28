"""The head/tail label sync's activities: which projects, and syncing one.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_sync_headtail_labels_activity.py and
get_headtail_label_studio_project_ids_activity. The cursor, concurrency and
heartbeat rules are the shared `sync_label_studio_project`'s, pinned in
test_label_sync.py; the row rules are the store's, pinned on Postgres
(test_head_tail_label_sync_store.py). Pinned here:

* v1's regression: a labeled task on hosted Label Studio (annotators as dicts)
  reaches the label, and its annotator is attributed -- v2 records the Label
  Studio id directly, so there is no user lookup left to fail;
* **v1 wrote a head/tail label only when its task had annotations** (unlike
  the laser sync), and v2 keeps that: a task with none writes nothing;
* the cursor kind is `head_tail` (v1's `headtail`, renamed by migrate-v1).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.headtail.sync import HeadTailSyncActivities
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import LabelProject

LAB, REEF = uuid.uuid4(), uuid.uuid4()
_HOSTED_ANNOTATOR = {"user_id": 141592, "annotated": True, "id": 141592}


def _kp(label, x, y):
    return {
        "from_name": "kp-1",
        "original_width": 100,
        "original_height": 200,
        "value": {"x": x, "y": y, "keypointlabels": [label]},
    }


def _task(task_id, *, labeled=True):
    return LabelStudioTask.from_sdk(
        SimpleNamespace(
            id=task_id,
            annotators=[_HOSTED_ANNOTATOR] if labeled else [],
            annotations=(
                [{"result": [_kp("Snout", 10, 20), _kp("Fork", 30, 40)]}]
                if labeled
                else []
            ),
            is_labeled=labeled,
            updated_at="2026-05-01T00:00:00Z",
        )
    )


class FakeLabelStudio:
    def __init__(self, tasks):
        self.tasks = tasks

    async def project_exists(self, project_id):
        return True

    async def list_tasks(self, project_id):
        return self.tasks


class FakeCatalog:
    def __init__(self, labelled_tasks=()):
        self.labelled = set(labelled_tasks)
        self.applied = []
        self.advanced = []
        self.kinds = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def label_studio_projects(self, tenant_id, kind):
        self.kinds.append(kind)
        return {LAB: [71, 76], REEF: [90]}[tenant_id]

    async def sync_cursor(self, tenant_id, kind, project_id):
        return None

    async def advance_sync_cursor(self, tenant_id, kind, project_id, at):
        self.advanced.append((tenant_id, kind, project_id))

    async def apply_head_tail_sync(self, tenant_id, ls_task_id, sync):
        if ls_task_id not in self.labelled:
            return False
        self.applied.append((tenant_id, ls_task_id, sync))
        return True


def _activities(catalog, tasks=()):
    return HeadTailSyncActivities(
        catalog=catalog, label_studio_factory=lambda: FakeLabelStudio(list(tasks))
    )


async def test_lists_every_served_tenants_head_tail_projects():
    catalog = FakeCatalog()

    projects = await ActivityEnvironment().run(
        _activities(catalog).head_tail_label_projects
    )

    assert projects == [
        LabelProject(LAB, 71),
        LabelProject(LAB, 76),
        LabelProject(REEF, 90),
    ]
    assert set(catalog.kinds) == {"head_tail"}


async def test_completion_syncs_when_annotators_are_hosted_ls_dicts():
    """v1's regression: the dict annotator was URL-encoded into a user lookup,
    422'd, and took the whole project's sync with it."""
    catalog = FakeCatalog(labelled_tasks={1})

    await ActivityEnvironment().run(
        _activities(catalog, [_task(1)]).sync_head_tail_labels, LabelProject(LAB, 71)
    )

    ((tenant, task_id, sync),) = catalog.applied
    assert (tenant, task_id) == (LAB, 1)
    assert sync.completed is True, "completion must reach the DB"
    assert sync.ls_labeler_id == 141592
    assert (sync.head_x, sync.tail_y) == (10.0, 80.0)
    assert catalog.advanced == [(LAB, "head_tail", 71)]


async def test_a_task_without_annotations_writes_nothing():
    """v1 PUT a head/tail label only when its task had annotations."""
    catalog = FakeCatalog(labelled_tasks={1, 2})

    await ActivityEnvironment().run(
        _activities(catalog, [_task(1, labeled=False), _task(2)]).sync_head_tail_labels,
        LabelProject(LAB, 71),
    )

    assert [task for _, task, _ in catalog.applied] == [2]
    assert catalog.advanced == [(LAB, "head_tail", 71)]


async def test_skips_tasks_with_no_existing_label():
    """Rows are created by populate; the sync only updates them."""
    catalog = FakeCatalog(labelled_tasks=())

    await ActivityEnvironment().run(
        _activities(catalog, [_task(i) for i in range(3)]).sync_head_tail_labels,
        LabelProject(LAB, 71),
    )

    assert catalog.applied == []
    assert catalog.advanced == [(LAB, "head_tail", 71)]
