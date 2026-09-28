"""The laser-depth and stage-14 orchestrator activities: select, resolve, persist.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_select_next_high_priority_dive_for_measure_fish_activity.py (v1 has no
test for the laser-depth selector; the map says write it first, and here it
is). v1's selectors were thin SDK calls, and the data-worker's activities did
their own reads and writes; in v2 the cohorts, the work and the persistence
rules are the stores' (fishsense-services-api tests/test_laser_depth_store.py,
test_measurement_store.py, on Postgres). What is pinned here is what the
activities add:

* **the oldest candidate across every tenant the orchestrator serves** --
  v1's first in, first out, kept across tenants so none can starve another;
* the resolver's output is the processing contract's input, in the store's
  order, and nothing at all when there is no work;
* the persist hands the store every depth and every refusal (v2: refusals
  are recorded, PLAN.md §9.16), the core that computed them, and the run;
* stage 14's persist reads species names with the contracts'
  `parse_species_names` -- v1's definition of record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from temporalio.testing import ActivityEnvironment

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
from fishsense_services_contracts import taxonomy
from fishsense_services_contracts.laser_depth import (
    ComputeLaserDepthsResult,
    LaserDepth,
    LaserDepthOutcome,
    LaserDepthRefusal,
    LaserDot,
)
from fishsense_services_contracts.measurement import (
    FishLength,
    HeadTail,
    MeasureFishResult,
)
from fishsense_services_orchestrator.laser_depth.activities import (
    DiveTarget,
    LaserDepthActivities,
)
from fishsense_services_orchestrator.measurement.activities import (
    MeasurementActivities,
)

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
LAB, REEF, PARTNER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
DIVE = uuid.uuid4()
K = ((3000.0, 0.0, 2048.0), (0.0, 3000.0, 1536.0), (0.0, 0.0, 1.0))
GEOMETRY = DiveGeometry(
    laser_calibration_id=uuid.uuid4(),
    laser_position=(0.1, 0.0, 0.0),
    laser_axis=(0.0, 0.0, 1.0),
    camera_calibration_id=uuid.uuid4(),
    camera_matrix=K,
)


class FakeCatalog:
    """Both stages' catalogs: the activities only call what they need."""

    def __init__(self, candidates=None, work=None):
        self.candidates = candidates or {}  # tenant -> candidate
        self.work = work
        self.persisted = None

    async def member_tenants(self):
        return [LAB, REEF, PARTNER]

    async def next_dive_for_laser_depth(self, tenant_id):
        return self.candidates.get(tenant_id)

    async def next_dive_for_measurement(self, tenant_id):
        return self.candidates.get(tenant_id)

    async def laser_depth_work(self, tenant_id, dive_id):
        return self.work

    async def measurement_work(self, tenant_id, dive_id):
        return self.work

    async def persist_laser_depths(self, tenant_id, dive_id, **result):
        self.persisted = (tenant_id, dive_id, result)
        return PersistedDepths(
            written=len(result["depths"]),
            refused=len(result["refusals"]),
            skipped_stale=0,
        )

    async def persist_measurements(self, tenant_id, dive_id, **result):
        self.persisted = (tenant_id, dive_id, result)
        return PersistedMeasurements(
            measured=len(result["lengths"]),
            refused=0,
            skipped_stale=0,
            fish_created=0,
            clusters_bound=0,
        )


def _candidates(kind):
    return {
        LAB: kind(uuid.UUID(int=1), T0 + timedelta(hours=2)),
        REEF: kind(uuid.UUID(int=2), T0),
        PARTNER: kind(uuid.UUID(int=3), T0 + timedelta(hours=1)),
    }


# -- selecting -------------------------------------------------------------------


async def test_the_depth_selector_takes_the_oldest_candidate_across_tenants():
    """v2. v1's selector was `ORDER BY id LIMIT 1` over one database; across
    tenants the oldest is chosen, so no tenant's backlog starves another's."""
    activities = LaserDepthActivities(
        catalog=FakeCatalog(candidates=_candidates(LaserDepthCandidate))
    )

    picked = await ActivityEnvironment().run(
        activities.select_next_dive_for_laser_depth
    )

    assert picked == DiveTarget(tenant_id=REEF, dive_id=uuid.UUID(int=2))


async def test_the_measure_selector_takes_the_oldest_candidate_across_tenants():
    activities = MeasurementActivities(
        catalog=FakeCatalog(candidates=_candidates(MeasurementCandidate))
    )

    picked = await ActivityEnvironment().run(
        activities.select_next_dive_for_measurement
    )

    assert picked == DiveTarget(tenant_id=REEF, dive_id=uuid.UUID(int=2))


async def test_the_selectors_return_none_when_every_cohort_is_empty():
    """v1: "returns None and ends if the cohort is empty"."""
    catalog = FakeCatalog()

    assert (
        await ActivityEnvironment().run(
            LaserDepthActivities(catalog=catalog).select_next_dive_for_laser_depth
        )
        is None
    )
    assert (
        await ActivityEnvironment().run(
            MeasurementActivities(catalog=catalog).select_next_dive_for_measurement
        )
        is None
    )


# -- resolving -------------------------------------------------------------------


async def test_the_depth_resolver_hands_the_processor_the_work_in_order():
    first, second = uuid.uuid4(), uuid.uuid4()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    work = LaserDepthWork(
        geometry=GEOMETRY,
        captures=[
            CaptureDots(first, [Dot(a, 1.0, 2.0), Dot(b, 3.0, 4.0)]),
            CaptureDots(second, [Dot(c, 5.0, 6.0)]),
        ],
        skipped_current=4,
        skipped_refused=1,
        skipped_unusable_label=2,
    )
    activities = LaserDepthActivities(catalog=FakeCatalog(work=work))

    resolution = await ActivityEnvironment().run(
        activities.resolve_laser_depth_inputs, DiveTarget(LAB, DIVE)
    )

    payload = resolution.payload
    assert payload.dive_id == DIVE
    assert payload.camera_matrix == K
    assert payload.calibration.laser_calibration_id == GEOMETRY.laser_calibration_id
    assert payload.calibration.laser_position == (0.1, 0.0, 0.0)
    assert [
        (cap.capture_id, [(d.laser_label_id, d.x, d.y) for d in cap.laser_labels])
        for cap in payload.captures
    ] == [(first, [(a, 1.0, 2.0), (b, 3.0, 4.0)]), (second, [(c, 5.0, 6.0)])]
    assert (
        resolution.skipped_current,
        resolution.skipped_refused,
        resolution.skipped_unusable_label,
    ) == (4, 1, 2)


async def test_the_depth_resolver_hands_nothing_when_there_is_no_work():
    work = LaserDepthWork(GEOMETRY, [], 3, 0, 0)
    activities = LaserDepthActivities(catalog=FakeCatalog(work=work))

    resolution = await ActivityEnvironment().run(
        activities.resolve_laser_depth_inputs, DiveTarget(LAB, DIVE)
    )

    assert resolution.payload is None
    assert resolution.skipped_current == 3


def _measure_capture(real_fish=True):
    return MeasureCapture(
        capture_id=uuid.uuid4(),
        species_label_id=uuid.uuid4(),
        content_of_image="Fish, Hogfish (Lachnolaimus maximus)",
        real_fish=real_fish,
        model_name=None,
        cluster_id=uuid.uuid4(),
        cluster_fish_id=None,
        laser=Dot(uuid.uuid4(), 1900.0, 1400.0),
        head_tail=HeadTailPoints(uuid.uuid4(), 1800.0, 1500.0, 2100.0, 1500.0),
    )


async def test_the_measure_resolver_hands_the_processor_one_laser_and_one_head_tail():
    items = [_measure_capture(), _measure_capture()]
    work = MeasurementWork(GEOMETRY, items, 5, 1, 2, 3, 4)
    activities = MeasurementActivities(catalog=FakeCatalog(work=work))

    resolution = await ActivityEnvironment().run(
        activities.resolve_measurement_inputs, DiveTarget(LAB, DIVE)
    )

    payload = resolution.payload
    assert payload.calibration.laser_calibration_id == GEOMETRY.laser_calibration_id
    assert [c.capture_id for c in payload.captures] == [i.capture_id for i in items]
    first = payload.captures[0]
    assert first.species_label_id == items[0].species_label_id
    assert first.laser == LaserDot(
        laser_label_id=items[0].laser.laser_label_id, x=1900.0, y=1400.0
    )
    assert first.head_tail == HeadTail(
        head_tail_label_id=items[0].head_tail.head_tail_label_id,
        head_x=1800.0,
        head_y=1500.0,
        tail_x=2100.0,
        tail_y=1500.0,
    )
    assert (
        resolution.skipped_already_measured,
        resolution.skipped_unmeasurable_species,
        resolution.missing_cluster,
        resolution.missing_laser_or_headtail,
        resolution.skipped_refused,
    ) == (5, 1, 2, 3, 4)


async def test_the_measure_resolver_hands_nothing_when_there_is_no_work():
    activities = MeasurementActivities(
        catalog=FakeCatalog(work=MeasurementWork(None, [], 0, 0, 0, 0, 0))
    )

    resolution = await ActivityEnvironment().run(
        activities.resolve_measurement_inputs, DiveTarget(LAB, DIVE)
    )

    assert resolution.payload is None


# -- persisting ------------------------------------------------------------------


async def test_the_depth_persist_hands_over_every_depth_and_refusal():
    catalog = FakeCatalog()
    capture, other = uuid.uuid4(), uuid.uuid4()
    good, bad, worse = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    result = ComputeLaserDepthsResult(
        dive_id=DIVE,
        core_version="4.1.0",
        captures=[
            LaserDepthOutcome(
                capture_id=capture,
                depth=LaserDepth(
                    laser_label_id=good, depth_m=1.2, range_m=1.25, residual_m=None
                ),
                refusals=[
                    LaserDepthRefusal(
                        laser_label_id=bad,
                        x=30.0,
                        y=3000.0,
                        reason="non_positive_depth",
                        depth_m=-0.4,
                    )
                ],
            ),
            LaserDepthOutcome(
                capture_id=other,
                depth=None,
                refusals=[
                    LaserDepthRefusal(
                        laser_label_id=worse,
                        x=1.0,
                        y=2.0,
                        reason="non_finite_depth",
                        depth_m=None,
                    )
                ],
            ),
        ],
    )
    calibration = uuid.uuid4()

    persisted = await ActivityEnvironment().run(
        LaserDepthActivities(catalog=catalog).persist_laser_depths,
        DiveTarget(LAB, DIVE),
        calibration,
        result,
    )

    tenant_id, dive_id, handed = catalog.persisted
    assert (tenant_id, dive_id) == (LAB, DIVE)
    assert handed["laser_calibration_id"] == calibration
    assert handed["core_version"] == "4.1.0"
    assert [(d.capture_id, d.laser_label_id, d.depth_m) for d in handed["depths"]] == [
        (capture, good, 1.2)
    ]
    assert [
        (r.capture_id, r.laser_label_id, r.reason, r.depth_m)
        for r in handed["refusals"]
    ] == [
        (capture, bad, "non_positive_depth", -0.4),
        (other, worse, "non_finite_depth", None),
    ]
    assert (persisted.computed, persisted.refused) == (1, 2)


async def test_the_measure_persist_hands_over_lengths_provenance_and_the_parser():
    catalog = FakeCatalog()
    item = _measure_capture()
    result = MeasureFishResult(
        dive_id=DIVE,
        algorithm="laser_depth_fronto_parallel",
        algorithm_version="1",
        core_version="4.1.0",
        captures=[
            FishLength(
                capture_id=item.capture_id,
                species_label_id=item.species_label_id,
                laser=LaserDot(laser_label_id=item.laser.laser_label_id, x=1.0, y=2.0),
                head_tail=HeadTail(
                    head_tail_label_id=item.head_tail.head_tail_label_id,
                    head_x=1.0,
                    head_y=2.0,
                    tail_x=3.0,
                    tail_y=4.0,
                ),
                length_m=None,
                depth_m=None,
                refusal="non_finite_length",
            )
        ],
    )
    calibration = uuid.uuid4()

    persisted = await ActivityEnvironment().run(
        MeasurementActivities(catalog=catalog).persist_measurements,
        DiveTarget(LAB, DIVE),
        calibration,
        result,
    )

    _, _, handed = catalog.persisted
    assert handed["laser_calibration_id"] == calibration
    assert (
        handed["algorithm"],
        handed["algorithm_version"],
        handed["core_version"],
    ) == ("laser_depth_fronto_parallel", "1", "4.1.0")
    assert handed["species_names"] is taxonomy.parse_species_names
    (length,) = handed["lengths"]
    assert (length.capture_id, length.refusal, length.length_m) == (
        item.capture_id,
        "non_finite_length",
        None,
    )
    assert length.laser == Dot(item.laser.laser_label_id, 1.0, 2.0)
    assert length.head_tail == HeadTailPoints(
        item.head_tail.head_tail_label_id, 1.0, 2.0, 3.0, 4.0
    )
    assert persisted.measured == 1
