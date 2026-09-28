"""The sweeper's Temporal visibility query, against a real Temporal server.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_scale_down_query_integration.py. The sweeper decides "is this
processor busy?" with a list-filter --
`TaskQueue = "<queue>" and (ExecutionStatus = "Running" or CloseTime >
"<now - cooldown>")`. Whether that string parses, and whether the visibility
store answers it correctly, is only found out against a real server.

v2 change: the server is Temporal's dev server, started by the test
(`WorkflowEnvironment.start_local`, as the schedule tests do) rather than the
devcontainer's, so it needs no stack and no marker. Visibility is eventually
consistent, so the assertions poll.
"""

from __future__ import annotations

import asyncio
import uuid

from temporalio.testing import WorkflowEnvironment

from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_orchestrator.nrp.activities import task_queue_busy

# Big enough that any workflow closed during this test counts as "recently
# closed", small enough to stay an int Temporal accepts.
_LONG_COOLDOWN_MIN = 10 * 365 * 24 * 60


async def _poll(predicate, *, timeout_s: float = 20.0, interval_s: float = 0.5) -> bool:
    """Poll an async predicate until True or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def test_busy_query_tracks_running_and_recently_closed_on_the_queue():
    async with await WorkflowEnvironment.start_local() as env:
        client = env.client

        async def _busy(cooldown: int = 0) -> bool:
            return await task_queue_busy(client, cooldown, PROCESSOR_TASK_QUEUE)

        async def _not_busy() -> bool:
            return not await _busy()

        assert await _poll(_not_busy), "expected the queue to be quiet to start"

        # The type need not be registered anywhere — `start_workflow` just
        # records the execution; with no worker it sits Running until we
        # terminate it.
        handle = await client.start_workflow(
            "TearDownProbeWorkflow",
            id=f"nrp-sweeper-it-{uuid.uuid4()}",
            task_queue=PROCESSOR_TASK_QUEUE,
        )
        try:
            assert await _poll(
                _busy
            ), f"the query should see the Running workflow on {PROCESSOR_TASK_QUEUE}"
        finally:
            await handle.terminate("end of test")

        # cooldown 0: a workflow that closed in the past is NOT "recent", so
        # the queue reads quiet again.
        assert await _poll(_not_busy), "after terminate, cooldown=0 should be quiet"
        # A huge cooldown: the just-terminated workflow IS within the window,
        # so the CloseTime clause fires and the queue reads busy.
        assert await _poll(
            lambda: _busy(_LONG_COOLDOWN_MIN)
        ), "a recently-closed workflow within the cooldown should count as busy"
