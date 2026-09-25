"""End to end: the orchestrator's image, under compose, against real Temporal.

Pins what only the deployed process can show: it starts from its environment,
connects to the right namespace, and polls the queue ingest is submitted to --
for workflows and for activities. There is no NAS here, so running an ingest
end to end is left to the lab-network run (PLAN.md §6.4).
"""

import asyncio
import time

import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError

pytestmark = pytest.mark.e2e

NAMESPACE = "fishsense"
QUEUE = "fishsense_orchestrator"


async def _pollers(address: str, kind: TaskQueueType.ValueType) -> int:
    client = await Client.connect(address, namespace=NAMESPACE)
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=NAMESPACE,
            task_queue=TaskQueue(name=QUEUE),
            task_queue_type=kind,
        )
    )
    return len(response.pollers)


@pytest.mark.parametrize(
    "kind",
    [TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY],
    ids=["workflows", "activities"],
)
def test_the_orchestrator_polls_its_queue(stack, kind):
    deadline = time.monotonic() + 60
    while (found := asyncio.run(_pollers(stack.temporal_address, kind))) == 0:
        if time.monotonic() > deadline:
            logs = stack.compose("logs", "--no-log-prefix", "orchestrator")
            pytest.fail(f"no pollers on {QUEUE}; orchestrator logs:\n{logs}")
        time.sleep(1)

    assert found >= 1


def test_the_orchestrator_runs_unprivileged_and_without_owner_credentials(stack):
    uid = stack.compose("exec", "-T", "orchestrator", "id", "-u").strip()
    environment = stack.compose("exec", "-T", "orchestrator", "env")

    assert uid != "0"
    assert "FISHSENSE_MIGRATION_DATABASE_URL" not in environment
    assert "owner-dev-only" not in environment


@pytest.mark.parametrize(
    "schedule_id", ["cluster-dive-frames", "sync-label-studio-laser-labels"]
)
def test_the_orchestrator_creates_its_schedules_at_startup(stack, schedule_id):
    """Each is created by the deployed worker, on its own queue."""

    async def describe():
        client = await Client.connect(stack.temporal_address, namespace=NAMESPACE)
        return await client.get_schedule_handle(schedule_id).describe()

    deadline = time.monotonic() + 60
    while True:
        try:
            schedule = asyncio.run(describe()).schedule
            break
        except RPCError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)

    assert schedule.action.task_queue == QUEUE
