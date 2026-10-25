"""The BioCLIP model-class replay as an orchestrator stage: workflows only (its
activities are the keypoint replay's plan and page), no schedule (run by
hand, `species_models.run`). Read-only."""

from __future__ import annotations

from fishsense_services_orchestrator.registry import Deps, Stage
from fishsense_services_orchestrator.species_models.workflow import (
    ReplaySpeciesModelsForMissionWorkflow,
    ReplaySpeciesModelsWorkflow,
)

__all__ = ["STAGE"]


def _activities(_deps: Deps):
    return []


STAGE = Stage(
    name="species_models",
    workflows=[ReplaySpeciesModelsWorkflow, ReplaySpeciesModelsForMissionWorkflow],
    build_activities=_activities,
    schedules=[],
)
