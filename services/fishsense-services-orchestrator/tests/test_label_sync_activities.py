"""The laser-label sync's activities: which projects, and syncing one.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_sync_laser_labels_activity.py (the activity half) and
get_laser_label_studio_project_ids_activity. v2: projects are listed across
every tenant the orchestrator serves, each with its tenant.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels.activities import LabelSyncActivities
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import LabelProject

LAB, REEF = uuid.uuid4(), uuid.uuid4()


def _task(task_id, x=10.0):
    raw = SimpleNamespace(
        id=task_id,
        annotators=[7],
        annotations=[{"result": [{
            "from_name": "kp-1", "original_width": 1000, "original_height": 800,
            "value": {"x": x, "y": 20.0, "keypointlabels": ["laser"]},
        }]}],
        is_labeled=True,
        updated_at="2026-05-01T00:00:00Z",
    )  # fmt: skip
    return LabelStudioTask.from_sdk(raw)


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

    async def member_tenants(self):
        return [LAB, REEF]

    async def label_studio_projects(self, tenant_id, kind):
        assert kind == "laser"
        return {LAB: [43, 44], REEF: [90]}[tenant_id]

    async def sync_cursor(self, tenant_id, kind, project_id):
        return None

    async def advance_sync_cursor(self, tenant_id, kind, project_id, at):
        self.advanced.append((tenant_id, kind, project_id))

    async def apply_laser_sync(self, tenant_id, ls_task_id, sync):
        if ls_task_id not in self.labelled:
            return False
        self.applied.append((tenant_id, ls_task_id, sync.x))
        return True


def _activities(catalog, tasks=()):
    return LabelSyncActivities(
        catalog=catalog, label_studio_factory=lambda: FakeLabelStudio(list(tasks))
    )


async def test_lists_every_served_tenants_laser_projects():
    projects = await ActivityEnvironment().run(
        _activities(FakeCatalog()).laser_label_projects
    )

    assert projects == [
        LabelProject(LAB, 43),
        LabelProject(LAB, 44),
        LabelProject(REEF, 90),
    ]


async def test_syncs_a_projects_tasks_into_its_tenants_labels():
    catalog = FakeCatalog(labelled_tasks={1, 2})

    await ActivityEnvironment().run(
        _activities(catalog, [_task(1), _task(2, x=50.0)]).sync_laser_labels,
        LabelProject(LAB, 43),
    )

    assert sorted(catalog.applied) == [(LAB, 1, 100.0), (LAB, 2, 500.0)]
    assert catalog.advanced == [(LAB, "laser", 43)]


async def test_skips_tasks_with_no_existing_label():
    """Labels are created when a project is populated; the sync only updates
    them, and a task with none is not an error."""
    catalog = FakeCatalog(labelled_tasks=())

    await ActivityEnvironment().run(
        _activities(catalog, [_task(i) for i in range(3)]).sync_laser_labels,
        LabelProject(LAB, 43),
    )

    assert catalog.applied == []
    assert catalog.advanced == [(LAB, "laser", 43)]
