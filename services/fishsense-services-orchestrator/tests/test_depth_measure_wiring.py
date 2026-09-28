"""The laser-depth and measure-fish stages across the orchestrator and the
processor, as they deploy.

The orchestrator's real parents and activities (with fake catalogs, whose
rules are tested on Postgres in the API), the processor's real light-role
worker with its real workflows and fishsense-core geometry, on their real
queues. Stubbed activities cannot catch a name the workflow calls that nothing
registers, or a payload that does not round-trip -- these do. The NRP wake is
the real activity, unconfigured: a no-op, as in compose and e2e.

The scene is v1's synthetic geometry (test_measure_fish_activity.py): a laser
dot, head and tail projected from a plane 1.2 m away, so the depth and the
length come back known.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import numpy as np
import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_api.laser_depth_store import (
    CaptureDots,
    DiveGeometry,
    Dot,
    LaserDepthCandidate,
    LaserDepthWork,
    PersistedDepths,
)
from fishsense_services_api.measurement_store import (
    HeadTailPoints,
    MeasureCapture,
    MeasurementCandidate,
    MeasurementWork,
    PersistedMeasurements,
)
from fishsense_services_orchestrator.laser_depth.activities import LaserDepthActivities
from fishsense_services_orchestrator.laser_depth.workflow import (
    ComputeLaserDepthsParentWorkflow,
)
from fishsense_services_orchestrator.measurement.activities import (
    MeasurementActivities,
)
from fishsense_services_orchestrator.measurement.workflow import (
    MeasureFishParentWorkflow,
)
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.worker import build_worker
from fishsense_services_processor.worker import build_worker as build_processor

TENANT, DIVE = uuid.uuid4(), uuid.uuid4()
CAMERA_MATRIX = np.array(
    [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
)
POSITION = np.array([-0.03, -0.10, 0.0])
AXIS = np.array([0.0, -0.02, 1.0]) / np.linalg.norm([0.0, -0.02, 1.0])
GEOMETRY = DiveGeometry(
    laser_calibration_id=uuid.uuid4(),
    laser_position=tuple(POSITION),
    laser_axis=tuple(AXIS),
    camera_calibration_id=uuid.uuid4(),
    camera_matrix=tuple(tuple(row) for row in CAMERA_MATRIX),
)


def _pixel(point) -> tuple[float, float]:
    p = CAMERA_MATRIX @ point
    return float(p[0] / p[2]), float(p[1] / p[2])


LASER = _pixel(POSITION + ((1.2 - POSITION[2]) / AXIS[2]) * AXIS)
HEAD, TAIL = _pixel(np.array([-0.10, 0.0, 1.2])), _pixel(np.array([0.20, 0.0, 1.2]))


def _activities_of(instance) -> list:
    return [
        getattr(instance, name)
        for name in dir(instance)
        if hasattr(getattr(instance, name), "__temporal_activity_definition")
    ]


class _Catalog:
    def __init__(self, work):
        self.work = work
        self.persisted = None

    async def member_tenants(self):
        return [TENANT]

    async def next_dive_for_laser_depth(self, tenant_id):
        return LaserDepthCandidate(DIVE, datetime(2025, 1, 1, tzinfo=UTC), 1)

    async def next_dive_for_measurement(self, tenant_id):
        return MeasurementCandidate(DIVE, datetime(2025, 1, 1, tzinfo=UTC), 1)

    async def laser_depth_work(self, tenant_id, dive_id):
        return self.work

    async def measurement_work(self, tenant_id, dive_id):
        return self.work

    async def persist_laser_depths(self, tenant_id, dive_id, **result):
        self.persisted = result
        return PersistedDepths(len(result["depths"]), len(result["refusals"]), 0)

    async def persist_measurements(self, tenant_id, dive_id, **result):
        self.persisted = result
        return PersistedMeasurements(len(result["lengths"]), 0, 0, 0, 0)


async def _run(parent, activities):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            build_worker(
                env.client,
                activities=[
                    *_activities_of(activities),
                    *_activities_of(NrpActivities(config=None)),
                ],
                task_queue="wiring",
            ),
            build_processor(env.client, role="light"),
        ):
            return await env.client.execute_workflow(
                parent.run, id=f"wiring-{uuid.uuid4()}", task_queue="wiring"
            )


async def test_laser_depths_run_across_the_orchestrator_and_the_processor():
    capture, good, bad = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    catalog = _Catalog(
        LaserDepthWork(
            geometry=GEOMETRY,
            # The first label is behind the camera; its sibling is the dot.
            captures=[
                CaptureDots(capture, [Dot(bad, 3000.0, 1600.0), Dot(good, *LASER)])
            ],
            skipped_current=0,
            skipped_refused=0,
            skipped_unusable_label=0,
        )
    )

    run = await _run(
        ComputeLaserDepthsParentWorkflow, LaserDepthActivities(catalog=catalog)
    )

    assert (run.computed, run.refused) == (1, 1)
    (depth,) = catalog.persisted["depths"]
    assert (depth.capture_id, depth.laser_label_id) == (capture, good)
    assert depth.depth_m == pytest.approx(1.2, abs=1e-3)
    (refusal,) = catalog.persisted["refusals"]
    assert (refusal.laser_label_id, refusal.reason) == (bad, "non_positive_depth")
    assert catalog.persisted["laser_calibration_id"] == GEOMETRY.laser_calibration_id
    assert catalog.persisted["core_version"] == "4.1.0"


async def test_fish_are_measured_across_the_orchestrator_and_the_processor():
    capture = uuid.uuid4()
    catalog = _Catalog(
        MeasurementWork(
            geometry=GEOMETRY,
            captures=[
                MeasureCapture(
                    capture_id=capture,
                    species_label_id=uuid.uuid4(),
                    content_of_image="Fish Model, Grouper",
                    real_fish=False,
                    model_name="Grouper",
                    cluster_id=None,
                    cluster_fish_id=None,
                    laser=Dot(uuid.uuid4(), *LASER),
                    head_tail=HeadTailPoints(uuid.uuid4(), *HEAD, *TAIL),
                )
            ],
            skipped_already_measured=0,
            skipped_unmeasurable_species=0,
            missing_cluster=0,
            missing_laser_or_headtail=0,
            skipped_refused=0,
        )
    )

    run = await _run(MeasureFishParentWorkflow, MeasurementActivities(catalog=catalog))

    assert run.measured == 1
    (length,) = catalog.persisted["lengths"]
    assert length.capture_id == capture
    # Within 1 mm of the constructed fish (v1's end-to-end tolerance).
    assert abs(length.length_m - 0.30) < 1e-3
    assert catalog.persisted["algorithm"] == "laser_depth_fronto_parallel"
