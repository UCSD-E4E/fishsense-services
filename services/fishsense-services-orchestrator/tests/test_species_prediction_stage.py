"""The species pre-annotation stage: what it serves, and that it ships off.

New in v2 (no v1 counterpart). Pinned here:

* **no schedule by default**: `FISHSENSE_SPECIES_PREDICTION_ENABLED` is false
  until an accuracy evaluation on FishSense frames turns it on. The workflows
  and activities are registered either way (the parent and the backfill can
  be run by hand);
* enabled, the predict parent fires hourly at +36 -- after head/tail predicts
  at +32, whose masks it crops -- skipping on overlap, with a run timeout
  covering every step at its longest (head/tail's rule).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_orchestrator.registry import Deps, stages
from fishsense_services_orchestrator.species_predict import workflow as wf
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)
from fishsense_services_orchestrator.species_predict.stage import (
    STAGE,
    species_prediction_schedules,
)

from .test_registry import NAS


def test_the_stage_is_discovered_and_serves_both_workflows():
    assert STAGE in stages()
    assert set(STAGE.workflows) == {
        wf.PredictSpeciesImagesParentWorkflow,
        wf.BackfillSpeciesPredictionsWorkflow,
    }


def test_it_ships_with_no_schedule():
    """The stage the worker registers, as imported under a default
    environment: nothing fires until someone turns it on."""
    assert list(STAGE.schedules) == []
    assert species_prediction_schedules(SpeciesPredictionSettings(enabled=False)) == []


def test_enabled_it_predicts_hourly_after_head_tail():
    (schedule,) = species_prediction_schedules(SpeciesPredictionSettings(enabled=True))

    assert schedule.schedule_id == "predict-species-images"
    assert schedule.workflow is wf.PredictSpeciesImagesParentWorkflow
    assert (schedule.every, schedule.offset) == (
        timedelta(hours=1),
        timedelta(minutes=36),
    )
    assert schedule.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.run_timeout == wf.PREDICT_RUN_TIMEOUT


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
        "select_next_dive_for_species_prediction",
        "resolve_species_predict_inputs",
        "persist_species_predictions",
        "backfill_species_predictions_for_dive",
    }


def _populate_owner(deps):
    from fishsense_services_orchestrator.species.stage import STAGE as SPECIES

    (populate,) = [
        a
        for a in SPECIES.build_activities(deps)
        if a.__temporal_activity_definition.name
        == "populate_species_label_studio_project"
    ]
    return populate.__self__


def test_species_populate_seeds_no_suggestion_by_default(deps, monkeypatch):
    """The other half of the gate: the species stage's populate is built
    with the switch as the environment sets it -- off."""
    monkeypatch.delenv("FISHSENSE_SPECIES_PREDICTION_ENABLED", raising=False)
    owner = _populate_owner(deps)
    # pylint: disable=protected-access
    assert owner._prediction_settings.enabled is False


def test_species_populate_follows_the_switch(deps, monkeypatch):
    monkeypatch.setenv("FISHSENSE_SPECIES_PREDICTION_ENABLED", "true")
    owner = _populate_owner(deps)
    # pylint: disable=protected-access
    assert owner._prediction_settings.enabled is True
    assert owner._predictions is not None
