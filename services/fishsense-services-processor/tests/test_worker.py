"""The processor's worker process: what it serves, and on which queue.

v1's data-worker registered its roles per deployment (roles.py); v2's processor
starts with the light role, stage 1 its only work. The test runs a real
clustering through the worker as built -- a workflow registered under the wrong
name, or on the wrong queue, would sit `Running` until its timeout with
nothing in the logs (v1's task_queues.py), and this catches both.
"""

import uuid
from datetime import UTC, datetime, timedelta

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_contracts import (
    PROCESSOR_LIGHT_TASK_QUEUE,
    ClusterDiveFrameImage,
    ClusterDiveFramesInput,
)
from fishsense_services_processor.worker import build_light_worker

BASE = datetime(2026, 5, 5, 10, 0, tzinfo=UTC)


async def test_the_light_worker_clusters_on_the_light_queue():
    payload = ClusterDiveFramesInput(
        dive_id=uuid.uuid4(),
        images=[
            ClusterDiveFrameImage(
                capture_id=uuid.UUID(int=i), taken_datetime=BASE + timedelta(seconds=i)
            )
            for i in range(3)
        ],
    )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with build_light_worker(env.client):
            clusters = await env.client.execute_workflow(
                "DiveFrameClusteringWorkflow",
                payload,
                id=f"cluster-{payload.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                result_type=list[list[uuid.UUID]],
            )

    assert sorted(c.int for cluster in clusters for c in cluster) == [0, 1, 2]
