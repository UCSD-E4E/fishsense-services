"""The slate presence detector (new in v2), as a stage.

**Ships disabled.** The detect schedule exists only when
`FISHSENSE_SLATE_DETECTION_ENABLED` is true (`settings`), read when the
worker imports its stages; the workflow and activities are registered either
way. Schedules are created if missing and never updated in place
(`schedules`), so turning the stage off again means deleting
`detect-slate-presence` as well.

Enabled, it fires hourly at +42 and drains dive after dive for up to 50
minutes (`workflow.DETECT_DRAIN_WINDOW`), oldest first across tenants and any
priority; stage 9 at +45 draws and queues the slate frames it finds in dives
with no slate labels.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.slate_presence_store import SlatePresenceCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage
from fishsense_services_orchestrator.slate_detect.activities import (
    SlateDetectionActivities,
)
from fishsense_services_orchestrator.slate_detect.settings import (
    SlateDetectionSettings,
)
from fishsense_services_orchestrator.slate_detect.workflow import (
    DETECT_RUN_TIMEOUT,
    DetectSlatePresenceParentWorkflow,
)

__all__ = ["STAGE", "slate_detection_schedules"]


def slate_detection_schedules(
    settings: SlateDetectionSettings,
) -> list[ScheduledWorkflow]:
    """The detect parent's schedule, or none while the stage is disabled."""
    if not settings.enabled:
        return []
    return [
        ScheduledWorkflow(
            schedule_id="detect-slate-presence",
            workflow=DetectSlatePresenceParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=42),
            run_timeout=DETECT_RUN_TIMEOUT,
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ]


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    activities = SlateDetectionActivities(
        catalog=SlatePresenceCatalog(deps.engine, sub=deps.sub),
        layout=ObjectLayout(ObjectStoreConnection()),
    )
    return [
        activities.select_next_dive_for_slate_detection,
        activities.resolve_slate_detection_inputs,
        activities.persist_slate_presence_predictions,
    ]


STAGE = Stage(
    name="slate_detect",
    workflows=[DetectSlatePresenceParentWorkflow],
    build_activities=_activities,
    schedules=slate_detection_schedules(SlateDetectionSettings()),
)
