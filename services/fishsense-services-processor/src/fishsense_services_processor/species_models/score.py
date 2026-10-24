"""One frame through BioCLIP twice, read-only (owner, 2026-10-23).

The crop is the automatic species step's (`species_predict.crop.crop_fish` on
the frame's SAM mask box). One image encoding is ranked against the species
set alone -- the control: what production answers -- and with the "Fish
Model" leaves added, each scored by its own prompts (a model has no
scientific name for the species templates). `p_model` is the model leaves'
summed probability in the second set.
"""

from __future__ import annotations

from typing import Any, Sequence

from fishsense_services_contracts.species_models import ModelCandidate
from fishsense_services_contracts.species_prediction import (
    SpeciesCandidate,
    SpeciesScore,
    species_prompts,
)
from fishsense_services_processor.species_predict.crop import crop_fish

__all__ = ["score_frame"]


def score_frame(
    jpeg: bytes,
    mask_bbox: Sequence[int],
    species: Sequence[SpeciesCandidate],
    models: Sequence[ModelCandidate],
    classifier: Any,
) -> dict:
    """The `SpeciesModelsResult` fields (without ids). Blocking."""
    crop = crop_fish(jpeg, mask_bbox)
    if crop is None:
        return {"status": "decode_failed", "model_id": classifier.model_id}
    species_set = [(c.choice, species_prompts(c.scientific_name)) for c in species]
    model_set = [(m.choice, m.prompts) for m in models]
    sets = classifier.score_sets(crop, {"species": species_set,
                                        "with_models": species_set + model_set})  # fmt: skip
    top = lambda r: [
        SpeciesScore(choice=c, probability=p) for c, p in r.top5
    ]  # noqa: E731
    (alone, _), (both, probs) = sets["species"], sets["with_models"]
    return {
        "status": "scored",
        "model_id": classifier.model_id,
        "species_top5": top(alone),
        "with_models_top5": top(both),
        "p_model": float(sum(probs[m.choice] for m in models)),
    }
