"""Stage 1's orchestrator activities: select, resolve, persist.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_select_next_high_priority_dive_for_clustering_activity.py,
test_resolve_dive_frame_clustering_inputs_activity.py and
test_persist_dive_frame_clusters_activity.py. v1's activities were thin SDK
calls; v2's call a catalog, and the cohort and persistence rules are the
store's, tested against Postgres (fishsense-services-api
tests/test_clustering_store.py). What is pinned here is what the activities
add:

* **v2: the oldest candidate across every tenant the orchestrator serves** --
  v1's first-in-first-out, kept across tenants so none can starve another;
* the resolver's output is the processing contract's input;
* a refusal of the processor's output is final: retrying re-reads the same
  clusters and reaches the same conclusion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.clustering_store import (
    ClusteringCandidate,
    ForeignCapture,
)
from fishsense_services_contracts import ClusterDiveFrameImage, ClusterDiveFramesInput
from fishsense_services_orchestrator.clustering.activities import (
    ClusteringActivities,
    ClusteringTarget,
)

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
LAB, REEF, PARTNER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class FakeCatalog:
    def __init__(self, candidates=None, captures=None, refuse=None):
        self.candidates = candidates or {}  # tenant -> ClusteringCandidate
        self.captures = captures or []
        self.refuse = refuse
        self.persisted = []

    async def member_tenants(self):
        return [LAB, REEF, PARTNER]

    async def next_dive_for_clustering(self, tenant_id):
        return self.candidates.get(tenant_id)

    async def canonical_capture_times(self, tenant_id, dive_id):
        return self.captures

    async def persist_prediction_clusters(self, tenant_id, dive_id, clusters):
        if self.refuse:
            raise self.refuse
        self.persisted.append((tenant_id, dive_id, clusters))
        return len(clusters)


def _activities(catalog):
    return ClusteringActivities(catalog=catalog)


async def test_picks_the_oldest_candidate_across_tenants():
    older, newer = uuid.uuid4(), uuid.uuid4()
    catalog = FakeCatalog(
        candidates={
            LAB: ClusteringCandidate(newer, T0 + timedelta(hours=1)),
            REEF: ClusteringCandidate(older, T0),
        }
    )

    target = await ActivityEnvironment().run(
        _activities(catalog).select_next_dive_for_clustering
    )

    assert target == ClusteringTarget(tenant_id=REEF, dive_id=older)


async def test_returns_none_when_no_tenant_has_a_candidate():
    target = await ActivityEnvironment().run(
        _activities(FakeCatalog()).select_next_dive_for_clustering
    )

    assert target is None


async def test_resolves_the_processing_contracts_input():
    """v1's `test_returns_image_id_taken_datetime_pairs`, on capture ids."""
    a, b = uuid.uuid4(), uuid.uuid4()
    dive = uuid.uuid4()
    catalog = FakeCatalog(captures=[(a, T0), (b, T0 + timedelta(seconds=1))])

    inputs = await ActivityEnvironment().run(
        _activities(catalog).resolve_clustering_inputs,
        ClusteringTarget(tenant_id=LAB, dive_id=dive),
    )

    assert inputs == ClusterDiveFramesInput(
        dive_id=dive,
        images=[
            ClusterDiveFrameImage(capture_id=a, taken_datetime=T0),
            ClusterDiveFrameImage(
                capture_id=b, taken_datetime=T0 + timedelta(seconds=1)
            ),
        ],
    )


async def test_returns_empty_image_list_when_dive_has_no_captures():
    inputs = await ActivityEnvironment().run(
        _activities(FakeCatalog()).resolve_clustering_inputs,
        ClusteringTarget(tenant_id=LAB, dive_id=uuid.uuid4()),
    )

    assert inputs.images == []


async def test_persists_the_clusters_for_the_targets_tenant():
    catalog = FakeCatalog()
    dive = uuid.uuid4()
    clusters = [[uuid.uuid4(), uuid.uuid4()], [uuid.uuid4()]]

    written = await ActivityEnvironment().run(
        _activities(catalog).persist_prediction_clusters,
        ClusteringTarget(tenant_id=LAB, dive_id=dive),
        clusters,
    )

    assert written == 2
    assert catalog.persisted == [(LAB, dive, clusters)]


async def test_a_refused_cluster_set_fails_without_retrying():
    catalog = FakeCatalog(refuse=ForeignCapture("not this dive's"))

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(catalog).persist_prediction_clusters,
            ClusteringTarget(tenant_id=LAB, dive_id=uuid.uuid4()),
            [[uuid.uuid4()]],
        )

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "InvalidClusters"
