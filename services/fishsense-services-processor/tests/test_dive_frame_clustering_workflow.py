"""`DiveFrameClusteringWorkflow` -- the processor's stage-1 workflow.

Ported from fishsense-lite@a8b2c3bc services/fishsense-data-processing-workflow-
worker/tests/test_dive_frame_clustering_workflow.py (the contract test, with a
stubbed activity) and test_stage1_integration.py (the real kernel on a real
worker registration -- here on Temporal's test server rather than the
devcontainer cluster). v2 adaptations: capture UUIDs, the pydantic converter.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, List
from uuid import UUID

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import ClusterDiveFrameImage, ClusterDiveFramesInput
from fishsense_services_processor.clustering.activities import cluster_dive_frames
from fishsense_services_processor.clustering.workflow import (
    DiveFrameClusteringWorkflow,
)

QUEUE = "test-stage1-clustering"
BASE = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)


async def _run(payload, activities):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[DiveFrameClusteringWorkflow],
            activities=activities,
        ):
            return await env.client.execute_workflow(
                DiveFrameClusteringWorkflow.run,
                payload,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


async def test_workflow_passes_images_to_activity_and_returns_result():
    captured: List[List[ClusterDiveFrameImage]] = []

    @activity.defn(name="cluster_dive_frames")
    async def stub_cluster(images: Iterable[ClusterDiveFrameImage]) -> List[List[UUID]]:
        materialized = list(images)
        captured.append(materialized)
        return [[img.capture_id for img in materialized]]

    payload = ClusterDiveFramesInput(
        dive_id=uuid.uuid4(),
        images=[
            ClusterDiveFrameImage(capture_id=UUID(int=1), taken_datetime=BASE),
            ClusterDiveFrameImage(
                capture_id=UUID(int=2), taken_datetime=BASE + timedelta(seconds=1)
            ),
        ],
    )

    result = await _run(payload, [stub_cluster])

    assert result == [[UUID(int=1), UUID(int=2)]]
    assert len(captured) == 1
    assert [img.capture_id for img in captured[0]] == [UUID(int=1), UUID(int=2)]


async def test_workflow_clusters_a_dive_end_to_end():
    """Two well-separated cohorts (5 images each, 10 minutes apart) reliably
    resolve into two HDBSCAN clusters with default parameters. Returned shape is
    `list[list[UUID]]` of capture ids -- what the orchestrator's persist
    activity turns into prediction clusters."""
    images = [
        ClusterDiveFrameImage(
            capture_id=UUID(int=10 * cohort + i),
            taken_datetime=BASE + timedelta(minutes=10 * cohort, seconds=i),
        )
        for cohort in range(2)
        for i in range(5)
    ]

    result = await _run(
        ClusterDiveFramesInput(dive_id=uuid.uuid4(), images=images),
        [cluster_dive_frames],
    )

    assert sorted(sorted(c.int for c in cluster) for cluster in result) == [
        [0, 1, 2, 3, 4],
        [10, 11, 12, 13, 14],
    ]
