"""Stage 13, checkerboard calibration and the lattice study, as a stage.

Schedules are v1's (fishsense-lite@77e8f8e5 worker.py): stage 13 hourly at +50
with a 30-minute run timeout, the checkerboard hourly at +52 with a 3 h run
timeout (staging a 133-frame folder takes ~30 minutes), both skipping on
overlap so two firings never fit one dive. The lattice study is never
scheduled: it is started by hand with the dives to study.
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.label_project_store import LabelProjectCatalog
from fishsense_services_api.laser_calibration_store import LaserCalibrationCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.calibration.activities import (
    LaserCalibrationActivities,
)
from fishsense_services_orchestrator.calibration.lattice import (
    LatticeProjectActivities,
)
from fishsense_services_orchestrator.calibration.workflows import (
    PerformCheckerboardCalibrationParentWorkflow,
    PerformLaserCalibrationParentWorkflow,
    VerifyCheckerboardLatticeParentWorkflow,
)
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.labels.populate import (
    LabelProjects,
    LabelStudioStorageSettings,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    # Read here, at the worker's start: a missing setting fails the start.
    store = OrchestratorObjectStore.from_settings(ObjectStoreConnection())
    label_studio_settings = LabelStudioSettings()
    label_studio = LabelStudioClient.from_settings(label_studio_settings)
    calibration = LaserCalibrationActivities(
        catalog=LaserCalibrationCatalog(deps.engine, sub=deps.sub), store=store
    )
    lattice = LatticeProjectActivities(
        label_projects=LabelProjects(
            catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
            label_studio=label_studio,
            workspace=label_studio_settings.workspace,
            storage=LabelStudioStorageSettings(),
        ),
        label_studio=label_studio,
    )
    return [
        calibration.select_next_dive_for_laser_calibration,
        calibration.select_next_dive_for_checkerboard_calibration,
        calibration.resolve_slate_calibration_inputs,
        calibration.resolve_checkerboard_calibration_inputs,
        calibration.record_laser_calibration,
        calibration.resolve_lattice_tenant,
        calibration.resolve_lattice_inputs,
        lattice.create_checkerboard_lattice_label_studio_project,
        lattice.populate_checkerboard_lattice_label_studio_project,
    ]


STAGE = Stage(
    name="calibration",
    workflows=[
        PerformLaserCalibrationParentWorkflow,
        PerformCheckerboardCalibrationParentWorkflow,
        VerifyCheckerboardLatticeParentWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        ScheduledWorkflow(
            schedule_id="perform-laser-calibration",
            workflow=PerformLaserCalibrationParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=50),
            run_timeout=timedelta(minutes=30),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
        ScheduledWorkflow(
            schedule_id="perform-checkerboard-calibration",
            workflow=PerformCheckerboardCalibrationParentWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=52),
            run_timeout=timedelta(hours=3),
            overlap=ScheduleOverlapPolicy.SKIP,
        ),
    ],
)
