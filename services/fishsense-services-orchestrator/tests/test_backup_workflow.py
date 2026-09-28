"""The nightly backup workflow, its schedule, and its worker's wiring.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/tests/
test_backup_databases_workflow.py, test_schedule.py and test_worker_wiring.py.
v1's rules, kept:

* one dump per database, then one prune per database;
* **a database is never pruned unless its own dump succeeded in this run**, or
  a failing dump plus a prune could leave fewer than `retention_count` good
  backups;
* the schedule is v1's cron (03:00 UTC daily), created at the worker's start
  if missing and never updated in place;
* the worker serves its own queue, with its own credentials: nothing else in
  the stack holds a role that can read every tenant's rows.

v2 changes, each pinned here:

* **one database's failed dump no longer stops the others.** v1 gathered the
  dumps without `return_exceptions`, so the first failure failed the workflow
  -- abandoning the other databases' dumps mid-flight and skipping every prune,
  despite its docstring's promise that databases were independent. v2 lets
  every dump finish, prunes the databases whose dump succeeded, and then fails,
  naming the ones that didn't;
* **the heartbeat pump is live.** v1's activities heartbeated every 30 s but
  the workflow set no heartbeat timeout, so a dead worker held a dump for its
  whole two-hour timeout; v2 sets one;
* the queue, schedule id and workflow id are v2's own, so a v2 worker never
  takes v1's backup (PLAN.md §6.5).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio import activity
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    WorkflowFailureError,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.ops.backup import worker as sut
from fishsense_services_orchestrator.ops.backup.schedule import (
    build_backup_schedule,
    ensure_schedule,
)
from fishsense_services_orchestrator.ops.backup.settings import (
    DEFAULT_TASK_QUEUE,
    BackupSettings,
)
from fishsense_services_orchestrator.ops.backup.workflow import (
    BackupDatabasesInput,
    BackupDatabasesWorkflow,
    PgDumpDatabaseInput,
    PruneDatabaseBackupsInput,
)

QUEUE = "test-backup"


def _stubs(timeline: List[str], *, fail_dump=()):
    @activity.defn(name="pg_dump_database")
    async def stub_pg_dump(payload: PgDumpDatabaseInput) -> None:
        timeline.append(f"dump:{payload.db_name}:{payload.nas_root_path}")
        if payload.db_name in fail_dump:
            raise ApplicationError(
                f"pg_dump failed for {payload.db_name}", non_retryable=True
            )

    @activity.defn(name="prune_database_backups")
    async def stub_prune(payload: PruneDatabaseBackupsInput) -> None:
        timeline.append(
            f"prune:{payload.db_name}:{payload.nas_root_path}:{payload.keep}"
        )

    return [stub_pg_dump, stub_prune]


async def _run(databases, *, fail_dump=(), history=False):
    timeline: List[str] = []
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[BackupDatabasesWorkflow],
            activities=_stubs(timeline, fail_dump=fail_dump),
        ):
            handle = await env.client.start_workflow(
                BackupDatabasesWorkflow.run,
                BackupDatabasesInput(
                    databases=databases,
                    nas_root_path="/fishsense_backups",
                    retention_count=14,
                ),
                id=f"backup-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            error = None
            try:
                await handle.result()
            except WorkflowFailureError as exc:
                error = exc
            events = (await handle.fetch_history()).events if history else None
    return timeline, error, events


# -- the workflow -----------------------------------------------------------------


async def test_workflow_dumps_each_db_then_prunes_each_db():
    timeline, error, _ = await _run(["fishsense_v2", "superset"])

    assert error is None
    assert sorted(e for e in timeline if e.startswith("dump:")) == [
        "dump:fishsense_v2:/fishsense_backups",
        "dump:superset:/fishsense_backups",
    ]
    assert sorted(e for e in timeline if e.startswith("prune:")) == [
        "prune:fishsense_v2:/fishsense_backups:14",
        "prune:superset:/fishsense_backups:14",
    ]


async def test_workflow_does_not_prune_until_all_dumps_complete():
    """A slow dump and a fast prune could otherwise drop a database below its
    retention during the run."""
    timeline, _, _ = await _run(["fishsense_v2", "superset"])

    last_dump = max(i for i, e in enumerate(timeline) if e.startswith("dump:"))
    first_prune = min(i for i, e in enumerate(timeline) if e.startswith("prune:"))
    assert last_dump < first_prune, timeline


async def test_workflow_with_no_databases_makes_no_activity_calls():
    timeline, error, _ = await _run([])

    assert error is None
    assert not timeline


async def test_a_failed_dump_does_not_stop_the_others_and_is_never_pruned():
    """v2: every dump runs to its end; the databases whose dump succeeded are
    pruned; the failed one keeps every backup it has; the run still fails, so
    the failure is seen."""
    timeline, error, _ = await _run(
        ["fishsense_v2", "superset", "other"], fail_dump=("superset",)
    )

    assert error is not None
    assert "superset" in str(error.cause)
    assert {e.split(":")[1] for e in timeline if e.startswith("dump:")} == {
        "fishsense_v2",
        "superset",
        "other",
    }
    assert {e.split(":")[1] for e in timeline if e.startswith("prune:")} == {
        "fishsense_v2",
        "other",
    }


async def test_a_cancelled_run_prunes_nothing_and_ends_cancelled():
    """Cancelling the run (an operator's `temporal workflow cancel`) is not a
    failed dump. v2 gathers the dumps with `return_exceptions`, which turns
    each dump's outcome into a value; had it also swallowed the run's own
    cancellation, the run would go on to prune the dumps that happened to
    finish, after being told to stop, and report a failure that never happened.
    It doesn't: a cancelled gather re-raises even with `return_exceptions`
    (asyncio's rule, which the workflow relies on rather than re-checking).
    Mutation-checked: catching the CancelledError around the gather fails this."""
    timeline: List[str] = []
    finished = asyncio.Event()

    @activity.defn(name="pg_dump_database")
    async def dump(payload: PgDumpDatabaseInput) -> None:
        timeline.append(f"dump:{payload.db_name}")
        if payload.db_name == "fast":
            finished.set()
            return
        while True:  # a long dump, heartbeating, until it is cancelled
            activity.heartbeat()
            await asyncio.sleep(0.05)

    @activity.defn(name="prune_database_backups")
    async def prune(payload: PruneDatabaseBackupsInput) -> None:
        timeline.append(f"prune:{payload.db_name}")

    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[BackupDatabasesWorkflow],
            activities=[dump, prune],
        ):
            handle = await env.client.start_workflow(
                BackupDatabasesWorkflow.run,
                BackupDatabasesInput(
                    databases=["fast", "slow"],
                    nas_root_path="/fishsense_backups",
                    retention_count=14,
                ),
                id=f"backup-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            await asyncio.wait_for(finished.wait(), timeout=30)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError) as raised:
                await handle.result()

    assert isinstance(raised.value.cause, CancelledError), raised.value.cause
    assert [e for e in timeline if e.startswith("prune:")] == []


async def test_the_dumps_have_v1s_timeout_and_a_live_heartbeat():
    _, _, events = await _run(["fishsense_v2"], history=True)

    scheduled = [
        e.activity_task_scheduled_event_attributes
        for e in events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    dump = next(s for s in scheduled if s.activity_type.name == "pg_dump_database")
    prune = next(
        s for s in scheduled if s.activity_type.name == "prune_database_backups"
    )
    assert dump.schedule_to_close_timeout.ToTimedelta() == timedelta(hours=2)
    assert dump.start_to_close_timeout.ToTimedelta() == timedelta(hours=2)
    # The pump beats every 30 s; a few missed beats means the worker is gone.
    assert dump.heartbeat_timeout.ToTimedelta() == timedelta(minutes=2)
    assert prune.schedule_to_close_timeout.ToTimedelta() == timedelta(minutes=10)
    assert prune.heartbeat_timeout.ToTimedelta() == timedelta(minutes=2)


# -- the schedule -----------------------------------------------------------------


def test_build_backup_schedule_wires_inputs_into_action():
    schedule = build_backup_schedule(
        databases=["fishsense_v2", "superset"],
        nas_root_path="/fishsense_backups",
        retention_count=14,
        cron_expression="0 3 * * *",
        task_queue="fishsense_backup",
    )

    assert isinstance(schedule, Schedule)
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.id == "BackupDatabasesWorkflow-workflow"
    assert action.task_queue == "fishsense_backup"
    assert schedule.spec.cron_expressions == ["0 3 * * *"]
    # Temporal's default, stated: a slow night never stacks a second dump.
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    (payload,) = action.args
    assert payload.databases == ["fishsense_v2", "superset"]
    assert payload.retention_count == 14


async def test_an_existing_schedule_is_left_as_it_is():
    """Create if missing, never update in place: a config typo must not
    silently change the production schedule. Delete and redeploy to change."""
    client = MagicMock()
    client.create_schedule = AsyncMock(
        side_effect=ScheduleAlreadyRunningError()  # pylint: disable=no-value-for-parameter
    )

    await ensure_schedule(client, schedule_id="backup-databases", schedule=MagicMock())

    client.create_schedule.assert_awaited_once()


def test_v2s_names_are_its_own():
    """Never v1's `fishsense_backup_queue` / `fishsense-daily-db-backup`, so a
    rehearsal worker can't take v1's backup (PLAN.md §6.5)."""
    settings = _settings()
    assert DEFAULT_TASK_QUEUE == settings.task_queue == "fishsense_backup"
    assert settings.schedule_id == "backup-databases"
    assert settings.schedule_cron == "0 3 * * *"
    assert settings.retention_count == 14


# -- the worker's wiring ----------------------------------------------------------

ENV = {
    "FISHSENSE_TEMPORAL_NAMESPACE": "fishsense",
    "FISHSENSE_NAS_URL": "https://nas.example.test:6021",
    "FISHSENSE_NAS_USERNAME": "u",
    "FISHSENSE_NAS_PASSWORD": "p",
    "FISHSENSE_BACKUP_DATABASE_HOST": "postgres",
    "FISHSENSE_BACKUP_DATABASE_USER": "fishsense_backup",
    "FISHSENSE_BACKUP_DATABASE_PASSWORD": "secret",
    "FISHSENSE_BACKUP_DATABASES": '["fishsense_v2", "superset"]',
    "FISHSENSE_BACKUP_NAS_ROOT_PATH": "/fishsense_backups",
}


def _settings() -> BackupSettings:
    return BackupSettings(
        database_host="postgres",
        database_user="fishsense_backup",
        database_password="secret",
        databases=["fishsense_v2"],
        nas_root_path="/fishsense_backups",
    )


@pytest.fixture
def wired(monkeypatch):
    """Run `main()` with Temporal replaced; hand back the recorded calls."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    client = MagicMock()
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(sut.Client, "connect", connect)
    ensure = AsyncMock()
    monkeypatch.setattr(sut, "ensure_schedule", ensure)
    instance = MagicMock()
    instance.run = AsyncMock()
    worker_cls = MagicMock(return_value=instance)
    monkeypatch.setattr(sut, "Worker", worker_cls)
    return connect, ensure, worker_cls, instance


async def test_registers_both_activities_and_the_workflow(wired):
    """A schedule that fires into a worker that can't run what the workflow
    calls fails at 03:00, on the job whose whole purpose is being the rollback
    mechanism."""
    _connect, _ensure, worker_cls, _instance = wired

    await sut.main()

    kwargs = worker_cls.call_args.kwargs
    assert {a.__temporal_activity_definition.name for a in kwargs["activities"]} == {
        "pg_dump_database",
        "prune_database_backups",
    }
    assert kwargs["workflows"] == [BackupDatabasesWorkflow]


async def test_listens_on_its_own_task_queue(wired):
    _connect, _ensure, worker_cls, _instance = wired

    await sut.main()

    assert worker_cls.call_args.kwargs["task_queue"] == "fishsense_backup"


async def test_registers_the_daily_schedule_idempotently_on_startup(wired):
    _connect, ensure, _worker_cls, _instance = wired

    await sut.main()

    ensure.assert_awaited_once()
    assert ensure.await_args.kwargs["schedule_id"] == "backup-databases"


async def test_the_schedule_carries_the_configured_databases_and_retention(wired):
    """The settings an operator actually changes must reach the schedule."""
    _connect, ensure, _worker_cls, _instance = wired

    await sut.main()

    (payload,) = ensure.await_args.kwargs["schedule"].action.args
    assert payload.databases == ["fishsense_v2", "superset"]
    assert payload.retention_count == 14
    assert payload.nas_root_path == "/fishsense_backups"


async def test_connects_with_the_shared_connection_settings(wired):
    """One Temporal connection for every service: the namespace is required,
    and the pydantic converter carries the payloads."""
    connect, _ensure, _worker_cls, _instance = wired

    await sut.main()

    kwargs = connect.await_args.kwargs
    assert kwargs["namespace"] == "fishsense"
    assert kwargs["data_converter"] is pydantic_data_converter


async def test_the_worker_is_actually_started(wired):
    _connect, _ensure, _worker_cls, instance = wired

    await sut.main()

    instance.run.assert_awaited_once()


async def test_a_missing_database_list_fails_the_start(wired, monkeypatch):
    """No default: the list must name v2's database, and a guess would back up
    the wrong one (or v1's) every night without complaint."""
    monkeypatch.delenv("FISHSENSE_BACKUP_DATABASES")

    with pytest.raises(Exception, match="databases"):
        await sut.main()


def test_run_drives_main_under_asyncio(monkeypatch):
    called = []
    monkeypatch.setattr(sut.asyncio, "run", called.append)
    monkeypatch.setattr(sut, "main", lambda: "coro")

    sut.run()

    assert called == ["coro"]
