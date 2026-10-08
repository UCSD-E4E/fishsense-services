"""The slate detector's stage: what it serves, and that it ships off.

New in v2 (the model is 2026-10-03_slate_detector@95a77d95's presence
classifier). Pinned here:

* **no schedule by default**: `FISHSENSE_SLATE_DETECTION_ENABLED` is false
  until someone turns it on. The workflow and activities are registered
  either way, so the parent can be run by hand;
* enabled, the detect parent fires hourly at +42 -- before stage 9 at +45,
  which draws and queues the frames it finds -- buffering one firing on overlap, with a
  run timeout covering every step at its longest;
* the API store's threshold is the contract's (the API does not import the
  contracts, so the two are pinned equal here).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_api.slate_presence_store import (
    SLATE_PRESENCE_THRESHOLD as STORE_THRESHOLD,
)
from fishsense_services_contracts.slate_presence import SLATE_PRESENCE_THRESHOLD
from fishsense_services_orchestrator.registry import Deps, stages
from fishsense_services_orchestrator.slate_detect import workflow as wf
from fishsense_services_orchestrator.slate_detect.settings import (
    SlateDetectionSettings,
)
from fishsense_services_orchestrator.slate_detect.stage import (
    STAGE,
    slate_detection_schedules,
)

from .test_registry import NAS


def test_the_stage_is_discovered_and_serves_the_parent():
    assert STAGE in stages()
    assert list(STAGE.workflows) == [wf.DetectSlatePresenceParentWorkflow]


def test_it_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FISHSENSE_SLATE_DETECTION_ENABLED", raising=False)
    assert SlateDetectionSettings().enabled is False


def test_it_ships_with_no_schedule():
    """The stage the worker registers, as imported under a default
    environment: nothing fires until someone turns it on."""
    assert list(STAGE.schedules) == []
    assert slate_detection_schedules(SlateDetectionSettings(enabled=False)) == []


def test_the_switch_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FISHSENSE_SLATE_DETECTION_ENABLED", "true")
    assert SlateDetectionSettings().enabled is True


def test_enabled_it_detects_hourly_before_stage_9():
    (schedule,) = slate_detection_schedules(SlateDetectionSettings(enabled=True))

    assert schedule.schedule_id == "detect-slate-presence"
    assert schedule.workflow is wf.DetectSlatePresenceParentWorkflow
    assert (schedule.every, schedule.offset) == (
        timedelta(hours=1),
        timedelta(minutes=42),
    )
    assert schedule.run_timeout == wf.DETECT_RUN_TIMEOUT


def test_a_firing_during_a_drain_runs_as_soon_as_it_ends():
    """A run drains for 50 minutes and then finishes its last dive, which can
    run past the next :42; skipped, that firing left the backlog idle for most
    of an hour (2026-10-07). One is buffered instead: it starts when the run
    ends, never alongside it, so two runs still never pick the same dive."""
    (schedule,) = slate_detection_schedules(SlateDetectionSettings(enabled=True))
    assert schedule.overlap == ScheduleOverlapPolicy.BUFFER_ONE


def test_the_stores_threshold_is_the_contracts():
    assert STORE_THRESHOLD == SLATE_PRESENCE_THRESHOLD


@pytest.fixture
def deps(monkeypatch) -> Deps:
    for name, value in NAS.items():
        monkeypatch.setenv(name, value)
    return Deps(engine=None, sub="service:fishsense-orchestrator")


def test_it_builds_every_activity(deps):
    names = {
        a.__temporal_activity_definition.name for a in STAGE.build_activities(deps)
    }
    assert names == {
        "select_next_dive_for_slate_detection",
        "resolve_slate_detection_inputs",
        "persist_slate_presence_predictions",
    }
