"""Operations, as a stage: checksum verification and path repair (on demand),
and the hourly labeling-config reconcile.

The nightly backup and the NRP cert sync are also ops, but not stages: each is
its own process, with credentials the orchestrator must never hold (see
`ops.backup` and `ops.cert_sync`).
"""

from datetime import timedelta

from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.capture_path_store import CapturePathCatalog
from fishsense_services_api.checksum_store import ChecksumCatalog
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioSettings,
)
from fishsense_services_orchestrator.ops.checksums.activities import (
    ChecksumActivities,
)
from fishsense_services_orchestrator.ops.checksums.workflows import (
    VerifyAllDivesChecksumsWorkflow,
    VerifyDiveChecksumsWorkflow,
)
from fishsense_services_orchestrator.ops.labeling_configs.activities import (
    LabelingConfigActivities,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    labeling_configs,
)
from fishsense_services_orchestrator.ops.labeling_configs.workflow import (
    ReconcileLabelingConfigsWorkflow,
)
from fishsense_services_orchestrator.ops.paths.activities import (
    PathRepairActivities,
)
from fishsense_services_orchestrator.ops.paths.workflow import (
    RepairMovedCapturePathsWorkflow,
)
from fishsense_services_orchestrator.registry import Deps, ScheduledWorkflow, Stage


def _activities(deps: Deps):
    nas = NasSettings()
    checksums = ChecksumActivities(
        catalog=ChecksumCatalog(deps.engine, sub=deps.sub), nas_settings=nas
    )
    paths = PathRepairActivities(
        catalog=CapturePathCatalog(deps.engine, sub=deps.sub), nas_settings=nas
    )
    label_studio = LabelStudioSettings()
    reconcile = LabelingConfigActivities(
        label_studio_factory=lambda: LabelStudioClient.from_settings(label_studio),
        workspace=label_studio.workspace,
        # Discovered here, at the worker's start: a malformed declaration
        # fails the start, not a run.
        configs=labeling_configs(),
    )
    return [
        checksums.verify_dive_checksums,
        checksums.select_canonical_dive_numbers,
        reconcile.reconcile_labeling_configs,
        paths.repair_moved_capture_paths,
    ]


STAGE = Stage(
    name="ops",
    workflows=[
        VerifyDiveChecksumsWorkflow,
        VerifyAllDivesChecksumsWorkflow,
        ReconcileLabelingConfigsWorkflow,
        RepairMovedCapturePathsWorkflow,
    ],
    build_activities=_activities,
    schedules=[
        # v1's :25, after the populates (:12, :20) have created the hour's
        # projects. Skips on overlap: a slow pass must not stack.
        ScheduledWorkflow(
            schedule_id="reconcile-labeling-configs",
            workflow=ReconcileLabelingConfigsWorkflow,
            every=timedelta(hours=1),
            offset=timedelta(minutes=25),
            run_timeout=timedelta(minutes=30),
            overlap=ScheduleOverlapPolicy.SKIP,
        )
    ],
)
