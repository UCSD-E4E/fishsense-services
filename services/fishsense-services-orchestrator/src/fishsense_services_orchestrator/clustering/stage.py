"""Stage 1, dive-frame clustering, as a stage."""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.clustering_store import ClusteringCatalog
from fishsense_services_orchestrator.clustering.activities import (
    ClusteringActivities,
)
from fishsense_services_orchestrator.clustering.workflow import (
    ClusterDiveFramesParentWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    clustering = ClusteringActivities(
        catalog=ClusteringCatalog(deps.engine, sub=deps.sub)
    )
    return [
        clustering.select_next_dive_for_clustering,
        clustering.resolve_clustering_inputs,
        clustering.persist_prediction_clusters,
    ]


STAGE = Stage(
    name="clustering",
    workflows=[ClusterDiveFramesParentWorkflow],
    build_activities=_activities,
    schedules=[
        # v1's :05. A selector skips on overlap, so two firings never pick
        # the same dive.
        ScheduledWorkflow(
            schedule_id="cluster-dive-frames",
            workflow=ClusterDiveFramesParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=5),
            run_timeout=timedelta(minutes=30),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ],
)
