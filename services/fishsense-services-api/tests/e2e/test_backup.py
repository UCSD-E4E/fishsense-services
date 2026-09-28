"""End to end: the backup's image, under compose, against real Temporal and the
real (migrated) database.

Pins what only the deployed process can show (fishsense-lite@77e8f8e5's backup
worker had only its unit tests and one local pg_dump): it starts from its
environment, registers the nightly schedule on its own queue and polls it, and
its image's pg_dump, as the role initdb creates, dumps v2's RLS-forced schema.
There is no NAS here, so the upload is left to the lab-network run.
"""

import asyncio
import time
from datetime import timedelta

import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError

pytestmark = pytest.mark.e2e

NAMESPACE = "fishsense"
QUEUE = "fishsense_backup"


def _eventually(check, *, what: str, stack, seconds: int = 60):
    deadline = time.monotonic() + seconds
    while True:
        try:
            result = check()
            if result:
                return result
        except RPCError:
            pass
        if time.monotonic() > deadline:
            logs = stack.compose("logs", "--no-log-prefix", "backup")
            pytest.fail(f"{what}; backup logs:\n{logs}")
        time.sleep(1)


def test_the_backup_registers_its_nightly_schedule_on_its_own_queue(stack):
    async def describe():
        client = await Client.connect(stack.temporal_address, namespace=NAMESPACE)
        return await client.get_schedule_handle("backup-databases").describe()

    described = _eventually(
        lambda: asyncio.run(describe()), what="no backup schedule", stack=stack
    )

    action = described.schedule.action
    assert action.workflow == "BackupDatabasesWorkflow"
    assert action.task_queue == QUEUE
    # Temporal hands a cron back as a calendar; v1's 03:00 UTC, daily.
    next_run, following, *_ = described.info.next_action_times
    assert (next_run.hour, next_run.minute) == (3, 0)
    assert following - next_run == timedelta(days=1)


def test_the_backup_polls_its_queue(stack):
    async def pollers():
        client = await Client.connect(stack.temporal_address, namespace=NAMESPACE)
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=NAMESPACE,
                task_queue=TaskQueue(name=QUEUE),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            )
        )
        return len(response.pollers)

    assert _eventually(
        lambda: asyncio.run(pollers()), what=f"no pollers on {QUEUE}", stack=stack
    )


def test_its_pg_dump_dumps_v2s_schema_as_the_backup_role(stack):
    """The image's pg_dump (at least the server's major), the role initdb
    created (BYPASSRLS), the migrated schema: a custom-format dump with the
    tenant tables' data in it."""
    toc = stack.compose(
        "exec", "-T", "backup", "sh", "-c",
        'PGPASSWORD="$FISHSENSE_BACKUP_DATABASE_PASSWORD" pg_dump -Fc '
        '-h "$FISHSENSE_BACKUP_DATABASE_HOST" -U "$FISHSENSE_BACKUP_DATABASE_USER" '
        "-d fishsense -f /tmp/e2e.dump && pg_restore -l /tmp/e2e.dump",
    )  # fmt: skip

    assert "TABLE DATA public tenants" in toc
    assert "TABLE DATA public captures" in toc


def test_only_the_backup_holds_the_backup_credential(stack):
    """The orchestrator and the API act for a tenant only as a member of it;
    neither may hold the role that reads every tenant."""
    uid = stack.compose("exec", "-T", "backup", "id", "-u").strip()
    for service in ("orchestrator", "api"):
        environment = stack.compose("exec", "-T", service, "env")
        assert "FISHSENSE_BACKUP_DATABASE_PASSWORD" not in environment
        assert "backup-dev-only" not in environment

    assert uid != "0"
