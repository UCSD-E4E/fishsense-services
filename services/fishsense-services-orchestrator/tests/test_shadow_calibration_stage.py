"""The shadow calibration stage: off by default, hourly when on, and its
workflows registered either way (so a mission can be run by hand)."""

from datetime import timedelta

from fishsense_services_orchestrator.shadow_calibration.settings import (
    ShadowCalibrationSettings,
)
from fishsense_services_orchestrator.shadow_calibration.stage import (
    STAGE,
    shadow_calibration_schedules,
)
from fishsense_services_orchestrator.shadow_calibration.workflow import (
    ShadowCalibrationForMissionWorkflow,
    ShadowCalibrationParentWorkflow,
)


def test_off_by_default():
    assert ShadowCalibrationSettings().enabled is False
    assert shadow_calibration_schedules(ShadowCalibrationSettings()) == []


def test_enabled_it_runs_hourly_and_skips_overlap():
    (schedule,) = shadow_calibration_schedules(ShadowCalibrationSettings(enabled=True))
    assert schedule.schedule_id == "shadow-calibration"
    assert schedule.workflow is ShadowCalibrationParentWorkflow
    assert schedule.every == timedelta(hours=1)
    assert schedule.overlap.name == "SKIP"


def test_workflows_registered_while_off():
    assert set(STAGE.workflows) == {ShadowCalibrationParentWorkflow,
                                    ShadowCalibrationForMissionWorkflow}  # fmt: skip
