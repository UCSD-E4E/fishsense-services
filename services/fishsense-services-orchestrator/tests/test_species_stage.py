"""The species stage: what it serves and when it fires.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_schedule_registration.py (the species schedules) and worker.py's
`schedule_workflows` (lines 473-479, 603-629). v1's intervals, minutes,
overlap policies and run timeouts are kept:

* stage-2 preprocess: hourly at +15, SKIP (two selectors must never pick the
  same dive), 2 h run timeout (its child may run 2 h);
* species populate: hourly at +20 -- just after +15 wrote the JPEGs -- SKIP,
  1 h;
* the species label sync: hourly on the hour, overlap allowed (a cursor only
  moves forward), 3 h -- v1's default, sized for a backlog project's first run.

v2 change: schedule ids drop v1's `-workflow-schedule` suffix, as the ported
stages' do, so they never collide with v1's while Temporal is shared.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleOverlapPolicy

from fishsense_services_orchestrator.registry import Deps, stages
from fishsense_services_orchestrator.species.stage import STAGE
from fishsense_services_orchestrator.species.workflows import (
    CreateSpeciesLabelStudioProjectWorkflow,
    PopulateSpeciesLabelStudioProjectParentWorkflow,
    PopulateSpeciesLabelStudioProjectWorkflow,
    PreprocessSpeciesImagesParentWorkflow,
    SyncLabelStudioSpeciesLabelsWorkflow,
    UpdateDiveImageGroupsWorkflow,
)

from .test_registry import NAS


def _schedule(schedule_id):
    return next(s for s in STAGE.schedules if s.schedule_id == schedule_id)


def test_the_species_stage_is_discovered():
    assert STAGE in stages()


def test_it_serves_every_species_workflow():
    assert set(STAGE.workflows) == {
        PreprocessSpeciesImagesParentWorkflow,
        CreateSpeciesLabelStudioProjectWorkflow,
        PopulateSpeciesLabelStudioProjectWorkflow,
        PopulateSpeciesLabelStudioProjectParentWorkflow,
        SyncLabelStudioSpeciesLabelsWorkflow,
        UpdateDiveImageGroupsWorkflow,
    }


def test_species_preprocess_is_scheduled_hourly_at_15():
    schedule = _schedule("preprocess-species-images")

    assert schedule.workflow is PreprocessSpeciesImagesParentWorkflow
    assert schedule.every == timedelta(hours=1)
    assert schedule.offset == timedelta(minutes=15)
    assert schedule.run_timeout == timedelta(hours=2)
    assert schedule.overlap is ScheduleOverlapPolicy.SKIP


def test_species_populate_is_scheduled_hourly_at_20():
    """The decoupled species-populate parent fires at +20, just after the +15
    species-preprocess writes JPEGs, with SKIP overlap like the other
    dive-selecting parents."""
    schedule = _schedule("populate-species-labels")

    assert schedule.workflow is PopulateSpeciesLabelStudioProjectParentWorkflow
    assert schedule.every == timedelta(hours=1)
    assert schedule.offset == timedelta(minutes=20)
    assert schedule.run_timeout == timedelta(hours=1)
    assert schedule.overlap is ScheduleOverlapPolicy.SKIP


def test_the_species_sync_is_scheduled_hourly_on_the_hour():
    schedule = _schedule("sync-label-studio-species-labels")

    assert schedule.workflow is SyncLabelStudioSpeciesLabelsWorkflow
    assert schedule.every == timedelta(hours=1)
    assert schedule.offset == timedelta(0)
    assert schedule.run_timeout == timedelta(hours=3)
    assert schedule.overlap is ScheduleOverlapPolicy.ALLOW_ALL


def test_on_demand_workflows_have_no_schedule():
    """6.1 runs per dive when its labeling is complete; create is manual."""
    scheduled = {s.workflow for s in STAGE.schedules}
    assert UpdateDiveImageGroupsWorkflow not in scheduled
    assert CreateSpeciesLabelStudioProjectWorkflow not in scheduled
    assert PopulateSpeciesLabelStudioProjectWorkflow not in scheduled


@pytest.fixture
def deps(monkeypatch) -> Deps:
    for name, value in NAS.items():
        monkeypatch.setenv(name, value)
    return Deps(engine=None, sub="service:fishsense-orchestrator")


def test_it_builds_every_species_activity(deps):
    names = {
        a.__temporal_activity_definition.name for a in STAGE.build_activities(deps)
    }

    assert names == {
        "select_next_dive_for_species_preprocessing",
        "resolve_species_preprocess_inputs",
        "clear_species_reprocess_flags",
        "create_species_label_studio_project",
        "select_dives_needing_species_population",
        "populate_species_label_studio_project",
        "species_label_projects",
        "sync_species_labels",
        "update_dive_image_groups",
    }
