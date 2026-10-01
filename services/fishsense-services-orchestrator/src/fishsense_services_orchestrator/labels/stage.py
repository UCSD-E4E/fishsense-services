"""The Label Studio label syncs, as a stage."""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.label_sync_store import LabelSyncCatalog
from fishsense_services_orchestrator.labels.activities import LabelSyncActivities
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.labels.workflow import (
    SyncLabelStudioLaserLabelsWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    settings = LabelStudioSettings()
    labels = LabelSyncActivities(
        catalog=LabelSyncCatalog(deps.engine, sub=deps.sub),
        label_studio_factory=lambda: LabelStudioClient.from_settings(settings),
    )
    return [labels.laser_label_projects, labels.sync_laser_labels]


STAGE = Stage(
    name="labels",
    workflows=[SyncLabelStudioLaserLabelsWorkflow],
    build_activities=_activities,
    schedules=[
        # v1's: hourly on the hour, sized for a first run over a backlog
        # project. Overlap is allowed (as in v1): a sync cursor only moves
        # forward, so two runs can't rewind each other.
        ScheduledWorkflow(
            schedule_id="sync-label-studio-laser-labels",
            workflow=SyncLabelStudioLaserLabelsWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(0),
            run_timeout=timedelta(hours=3),
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        )
    ],
)
