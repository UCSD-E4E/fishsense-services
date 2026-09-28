"""The processor on NRP, as a stage: the wakes the parents call, and the sweeper.

The config resolves when the worker starts (``FISHSENSE_NRP_*``); without a
kubeconfig every activity is a no-op, as locally and in e2e.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.nrp.scaling import resolve_scaling_config
from fishsense_services_orchestrator.nrp.workflow import (
    TearDownIdleProcessorsWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(_deps: Deps):
    nrp = NrpActivities(config=resolve_scaling_config())
    return [
        nrp.ensure_per_image_processor_running,
        nrp.ensure_light_processor_running,
        nrp.ensure_gpu_processor_running,
        nrp.tear_down_idle_processors,
    ]


STAGE = Stage(
    name="nrp",
    workflows=[TearDownIdleProcessorsWorkflow],
    build_activities=_activities,
    schedules=[
        # v1's +55: after the hour's parents have fired, so the sweep doesn't
        # race one still standing a processor up. Skips on overlap.
        ScheduledWorkflow(
            schedule_id="tear-down-idle-processors",
            workflow=TearDownIdleProcessorsWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=55),
            run_timeout=timedelta(minutes=10),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ],
)
