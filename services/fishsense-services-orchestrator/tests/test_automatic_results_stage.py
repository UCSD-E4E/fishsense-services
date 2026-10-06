"""The automatic-results stage: off by default, hourly when on."""

from datetime import timedelta

from fishsense_services_orchestrator.automatic_results.settings import (
    AutomaticResultsSettings,
)
from fishsense_services_orchestrator.automatic_results.stage import (
    automatic_results_schedules,
)
from fishsense_services_orchestrator.automatic_results.workflow import (
    AutomaticResultsParentWorkflow,
)


def test_off_by_default():
    assert AutomaticResultsSettings().enabled is False
    assert automatic_results_schedules(AutomaticResultsSettings()) == []


def test_enabled_it_runs_hourly_and_skips_overlap():
    (schedule,) = automatic_results_schedules(AutomaticResultsSettings(enabled=True))
    assert schedule.workflow is AutomaticResultsParentWorkflow
    assert schedule.every == timedelta(hours=1)
    assert schedule.overlap.name == "SKIP"
