"""BioCLIP with fish-model classes, read-only (v20, owner 2026-10-23).

BioCLIP's closed set is the species config's "Fish" leaves, so a plastic fish
model is always named as some real species (2,591 human-labeled model frames:
mutton snapper 1,299, goliath grouper 266, ...). This replay scores each
automatic frame's crop twice -- against that set alone (the control: what
production answers) and with the "Fish Model" leaves added, each by prompts of
its own -- to see whether BioCLIP can tell a model from a fish before any
pipeline relies on it. Nothing is stored. New models only."""

from __future__ import annotations

from typing import Annotated, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.headtail_keypoints import KeypointFrame
from fishsense_services_contracts.renamed import renamed_from
from fishsense_services_contracts.species_prediction import (
    SpeciesCandidate,
    SpeciesScore,
)

__all__ = ["ModelCandidate", "SpeciesModelsInput", "SpeciesModelsResult"]


class ModelCandidate(BaseModel):
    """A "Fish Model" choice, scored by its own prompts (a model has no
    scientific name for the species templates)."""

    choice: str
    prompts: List[str]


class SpeciesModelsInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    species: List[SpeciesCandidate]
    models: List[ModelCandidate]
    #: Each frame's render (presigned GET) and its SAM mask box.
    frames: List[KeypointFrame]


class SpeciesModelsResult(BaseModel):
    """`status`: ``scored``, ``render_unavailable``, ``decode_failed``,
    ``failed``."""

    prediction_id: UUID
    capture_id: UUID
    status: str
    model_id: Optional[str] = None
    species_top5: List[SpeciesScore] = []
    with_models_top5: List[SpeciesScore] = []
    #: The model classes' summed probability with them in the set.
    p_model: Optional[float] = None
