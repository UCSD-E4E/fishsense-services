"""The project sync (cursor, concurrency, heartbeats) and the laser reading.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_sync_cursor_behavior.py and test_sync_laser_labels_activity.py. Names,
bodies and reasons are v1's; the harness changed (a fake Label Studio adapter
and a fake catalog in place of v1's two mocked clients), and v2 adaptations are
marked: the project belongs to a tenant, and the cursor's forward-only rule is
the store's (tested against Postgres).

The cursor contract:

* with no cursor, every task is processed and the cursor moves to
  `max(task.updated_at)`;
* with a cursor, only strictly-newer tasks are processed;
* on any per-task failure the cursor does NOT move (replay is safe: applying
  a task is an update);
* each kind keeps its own cursor per project.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, List

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.label_sync_store import LaserSync
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import (
    SYNC_CONCURRENCY,
    LabelProject,
    laser_sync_from_task,
    sync_label_studio_project,
)

TENANT = uuid.uuid4()
PROJECT = LabelProject(tenant_id=TENANT, ls_project_id=42)


def _task(task_id: int, *, updated_at: str | None = "2026-05-01T00:00:00Z") -> Any:
    return LabelStudioTask.from_sdk(
        type("Raw", (), {"id": task_id, "updated_at": updated_at})()
    )


class FakeLabelStudio:
    def __init__(self, tasks, *, exists=True):
        self.tasks = tasks
        self.exists = exists
        self.listed = False

    async def project_exists(self, project_id):
        return self.exists

    async def list_tasks(self, project_id):
        self.listed = True
        return self.tasks


class FakeCatalog:
    def __init__(self, *, cursor=None):
        self.cursor = cursor
        self.cursor_reads: list[tuple] = []
        self.advanced: list[tuple] = []

    async def sync_cursor(self, tenant_id, kind, project_id):
        self.cursor_reads.append((tenant_id, kind, project_id))
        return self.cursor

    async def advance_sync_cursor(self, tenant_id, kind, project_id, at):
        self.advanced.append((tenant_id, kind, project_id, at))


async def _sync(tasks, apply, *, cursor=None, kind="laser", exists=True, env=None):
    ls, catalog = FakeLabelStudio(tasks, exists=exists), FakeCatalog(cursor=cursor)

    @activity.defn(name="_test")
    async def stub_activity():
        await sync_label_studio_project(PROJECT, kind, ls=ls, catalog=catalog,
                                        apply=apply)  # fmt: skip

    await (env or ActivityEnvironment()).run(stub_activity)
    return ls, catalog


# -- the cursor (v1's test_sync_cursor_behavior.py) ------------------------------


async def test_no_cursor_processes_every_task_and_writes_max_seen():
    tasks = [
        _task(1, updated_at="2026-04-01T00:00:00Z"),
        _task(2, updated_at="2026-04-15T00:00:00Z"),
        _task(3, updated_at="2026-04-10T00:00:00Z"),
    ]
    processed: List[int] = []

    async def apply(task):
        processed.append(task.id)

    _, catalog = await _sync(tasks, apply)

    assert sorted(processed) == [1, 2, 3]
    assert catalog.advanced == [
        (TENANT, "laser", 42, datetime(2026, 4, 15, tzinfo=UTC))
    ]


async def test_cursor_filters_out_tasks_at_or_before_high_water():
    tasks = [
        _task(1, updated_at="2026-04-01T00:00:00Z"),
        _task(2, updated_at="2026-04-10T00:00:00Z"),  # == cursor: skip
        _task(3, updated_at="2026-04-12T00:00:00Z"),
        _task(4, updated_at="2026-04-09T00:00:00Z"),  # < cursor: skip
    ]
    processed: List[int] = []

    async def apply(task):
        processed.append(task.id)

    _, catalog = await _sync(tasks, apply, cursor=datetime(2026, 4, 10, tzinfo=UTC))

    assert processed == [3]
    assert catalog.advanced == [
        (TENANT, "laser", 42, datetime(2026, 4, 12, tzinfo=UTC))
    ]


async def test_a_task_without_a_timestamp_is_always_processed():
    """No comparable timestamp conservatively means "process it"."""
    processed: List[int] = []

    async def apply(task):
        processed.append(task.id)

    await _sync([_task(1, updated_at=None)], apply,
                cursor=datetime(2026, 4, 10, tzinfo=UTC))  # fmt: skip

    assert processed == [1]


async def test_cursor_not_written_when_no_new_tasks():
    async def apply(task):
        raise AssertionError("apply should not be invoked")

    _, catalog = await _sync(
        [_task(1, updated_at="2026-04-01T00:00:00Z")],
        apply,
        cursor=datetime(2026, 4, 10, tzinfo=UTC),
    )

    assert catalog.advanced == []


async def test_cursor_not_advanced_on_per_task_failure():
    async def apply(task):
        if task.id == 2:
            raise RuntimeError("simulated downstream failure")

    ls, catalog = FakeLabelStudio(
        [_task(1, updated_at="2026-04-01T00:00:00Z"),
         _task(2, updated_at="2026-04-15T00:00:00Z")]
    ), FakeCatalog()  # fmt: skip

    @activity.defn(name="_test")
    async def stub_activity():
        await sync_label_studio_project(PROJECT, "laser", ls=ls, catalog=catalog,
                                        apply=apply)  # fmt: skip

    with pytest.raises(BaseException):  # ExceptionGroup or RuntimeError
        await ActivityEnvironment().run(stub_activity)

    assert catalog.advanced == []


async def test_kind_is_forwarded_to_cursor_calls():
    async def apply(task):
        return None

    _, catalog = await _sync([_task(1)], apply, kind="head_tail")

    assert catalog.cursor_reads == [(TENANT, "head_tail", 42)]
    assert catalog.advanced[0][:3] == (TENANT, "head_tail", 42)


# -- the project (v1's test_sync_laser_labels_activity.py) -----------------------


async def test_returns_early_when_project_missing():
    async def apply(task):
        raise AssertionError("apply should not be invoked")

    ls, catalog = await _sync([_task(1)], apply, exists=False)

    assert ls.listed is False
    assert catalog.advanced == []


async def test_per_task_concurrency_is_bounded_by_semaphore():
    """The Phase 1 regression guard: with N >> SYNC_CONCURRENCY tasks,
    never more than SYNC_CONCURRENCY applies in flight. Pre-fix this was
    unbounded, and caused TaskGroup timeouts in prod."""
    n_tasks = 50
    in_flight = 0
    peak_in_flight = 0

    async def apply(task):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        # Yield so other coroutines contend for the semaphore; unenforced,
        # peak would jump straight to n_tasks.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight -= 1

    await _sync([_task(i) for i in range(n_tasks)], apply)

    assert (
        peak_in_flight <= SYNC_CONCURRENCY
    ), f"peak concurrency was {peak_in_flight}, expected <= {SYNC_CONCURRENCY}"


async def test_heartbeat_fires_per_completed_task():
    n_tasks = 5
    heartbeats: List[tuple] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: heartbeats.append(args)

    async def apply(task):
        return None

    await _sync([_task(i) for i in range(n_tasks)], apply, env=env)

    assert len(heartbeats) == n_tasks


# -- reading a laser task (v1's __update_laser_label, as a pure function) --------


def _laser_task(result, *, is_labeled=True, annotator=141592):
    raw = type(
        "Raw",
        (),
        {
            "id": 7,
            "annotators": [{"user_id": annotator}] if annotator else [],
            "annotations": [{"result": result}] if result is not None else [],
            "is_labeled": is_labeled,
            "updated_at": "2026-05-01T00:00:00Z",
        },
    )()
    return LabelStudioTask.from_sdk(raw)


def _keypoint(from_name="kp-1", x=10.0, y=20.0, label="laser"):
    return {
        "from_name": from_name,
        "original_width": 1000,
        "original_height": 800,
        "value": {"x": x, "y": y, "keypointlabels": [label]},
    }


def test_a_keypoint_is_converted_from_percent_to_pixels():
    """Label Studio stores keypoints as percentages of the image."""
    sync = laser_sync_from_task(_laser_task([_keypoint()]))

    assert sync == LaserSync(
        completed=True, x=100.0, y=160.0, label="laser", ls_labeler_id=141592,
        ls_updated_at=datetime(2026, 5, 1, tzinfo=UTC), ls_payload={},
    )  # fmt: skip


def test_the_older_laser_key_name_is_read_too():
    """Label configs have used both `kp-1` and `laser` as the control name."""
    sync = laser_sync_from_task(_laser_task([_keypoint(from_name="laser")]))

    assert (sync.x, sync.y) == (100.0, 160.0)


def test_other_controls_are_ignored():
    sync = laser_sync_from_task(_laser_task([_keypoint(from_name="something-else")]))

    assert (sync.x, sync.y, sync.label) == (None, None, None)


def test_an_unannotated_task_is_incomplete_with_no_point():
    sync = laser_sync_from_task(_laser_task(None, is_labeled=False, annotator=None))

    assert (sync.completed, sync.x, sync.y, sync.ls_labeler_id) == (False, None, None,
                                                                     None)  # fmt: skip
