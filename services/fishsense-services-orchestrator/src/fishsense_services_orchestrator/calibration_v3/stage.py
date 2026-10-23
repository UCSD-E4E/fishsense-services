"""Calibration v3's replay as an orchestrator stage: no schedule (run by hand,
`calibration_v3.replay`)."""

from __future__ import annotations

from fishsense_services_api.automatic_results_store import AutomaticResultsCatalog
from fishsense_services_api.slate_store import SlateCatalog
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.calibration_v3.activities import (
    CalibrationV3Activities,
)
from fishsense_services_orchestrator.calibration_v3.workflow import (
    ReplayCalibrationV3ForMissionWorkflow,
    ReplayCalibrationV3Workflow,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.registry import Deps, Stage

__all__ = ["STAGE"]


def _activities(deps: Deps):
    acts = CalibrationV3Activities(
        catalog=AutomaticResultsCatalog(deps.engine, sub=deps.sub),
        slates=SlateCatalog(deps.engine, sub=deps.sub),
        layout=ObjectLayout(ObjectStoreConnection()),
    )
    return [acts.plan_calibration_v3_replay, acts.resolve_mission_slate_template]


STAGE = Stage(
    name="calibration_v3",
    workflows=[ReplayCalibrationV3Workflow, ReplayCalibrationV3ForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
