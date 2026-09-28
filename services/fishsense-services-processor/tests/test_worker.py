"""The processor's worker process: what it serves, and on which queue.

v1's data-worker chose its role from settings (fishsense-lite@77e8f8e5
roles.py, `general.role`); v2's reads ``FISHSENSE_PROCESSOR_ROLE``, and every
NRP manifest sets it. The wiring test runs a real clustering through the worker
as built -- a workflow registered under the wrong name, or on the wrong queue,
would sit `Running` until its timeout with nothing in the logs (v1's
task_queues.py), and this catches both.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_contracts import (
    PROCESSOR_LIGHT_TASK_QUEUE,
    ClusterDiveFrameImage,
    ClusterDiveFramesInput,
)
from fishsense_services_processor.worker import ProcessorSettings, build_worker

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
        async with build_worker(env.client, role="light"):
            clusters = await env.client.execute_workflow(
                "DiveFrameClusteringWorkflow",
                payload,
                id=f"cluster-{payload.dive_id}",
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                result_type=list[list[uuid.UUID]],
            )

    assert sorted(c.int for cluster in clusters for c in cluster) == [0, 1, 2]


def test_the_role_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("FISHSENSE_PROCESSOR_ROLE", "gpu")
    monkeypatch.setenv("FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES", "1")

    settings = ProcessorSettings()

    assert settings.role == "gpu"
    assert settings.max_concurrent_activities == 1


def test_the_role_is_required(monkeypatch):
    """No default: a pod whose manifest forgot its role must fail at startup,
    not quietly serve the light queue while its own sits unpolled."""
    monkeypatch.delenv("FISHSENSE_PROCESSOR_ROLE", raising=False)
    with pytest.raises(ValidationError):
        ProcessorSettings()


@pytest.mark.parametrize("cap", ["0", "-1"])
def test_a_cap_must_be_positive(monkeypatch, cap):
    monkeypatch.setenv("FISHSENSE_PROCESSOR_ROLE", "light")
    monkeypatch.setenv("FISHSENSE_PROCESSOR_MAX_CONCURRENT_ACTIVITIES", cap)
    with pytest.raises(ValidationError):
        ProcessorSettings()
