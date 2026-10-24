"""The model classes BioCLIP is offered in the replay: the species config's
"Fish Model" leaves, each scored by prompts of its own (a model has no
scientific name for the species templates). The prompts name the object, not
where it is: models turn up outside the pool too (owner, 2026-10-23)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from fishsense_services_contracts.species_models import ModelCandidate
from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.species_predict.candidates import SPECIES_CONTROL

__all__ = [
    "MODEL_BRANCH",
    "MODEL_DESCRIPTIONS",
    "MODEL_PROMPT_TEMPLATES",
    "model_candidates",
]

MODEL_BRANCH = "Fish Model"
#: What each model is, in words BioCLIP's text side can place.
MODEL_DESCRIPTIONS = {
    "Weasly Fish": "a plastic toy fish",
    "Snook": "a plastic model of a snook",
    "Grouper": "a plastic model of a grouper",
    "Shark": "a plastic toy shark",
    "Gray Anthias": "a plastic model of a gray anthias",
    "Purple Angel": "a plastic model of a purple angelfish",
    "Yellow Anthias": "a plastic model of a yellow anthias",
}
MODEL_PROMPT_TEMPLATES = (
    "a photo of {d}",
    "an underwater photo of {d}",
    "{d}, an artificial fish",
    "{d} made of plastic",
)


def model_candidates(xml: str = SPECIES_LABELING_CONFIG_XML) -> list[ModelCandidate]:
    """The config's "Fish Model" leaves, in its order; a leaf with no
    description is an error, not a silent omission."""
    root = ET.fromstring(xml)
    (taxonomy,) = [t for t in root.iter("Taxonomy") if t.get("name") == SPECIES_CONTROL]
    (branch,) = [
        c for c in taxonomy.findall("Choice") if c.get("value") == MODEL_BRANCH
    ]
    out = []
    for leaf in branch.findall("Choice"):
        name = leaf.get("value")
        if name not in MODEL_DESCRIPTIONS:
            raise ValueError(f"a Fish Model leaf with no description: {name!r}")
        d = MODEL_DESCRIPTIONS[name]
        out.append(ModelCandidate(choice=f"{MODEL_BRANCH}, {name}",
                                  prompts=[t.format(d=d) for t in MODEL_PROMPT_TEMPLATES]))  # fmt: skip
    return out
