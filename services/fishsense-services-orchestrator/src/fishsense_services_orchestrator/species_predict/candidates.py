"""What BioCLIP may answer: the species labeling config's target species.

New in v2 (no v1 counterpart). The candidates are derived from the species
project's labeling config (`species.labeling.SPECIES_LABELING_CONFIG_XML`),
not listed again: every leaf of the species Taxonomy's "Fish" branch, given to
BioCLIP by its scientific name (`taxonomy.parse_species_names` reads
"Common (Genus species)"), except the two leaves that name no species. So the
closed set is exactly what a labeler can pick, and adding a species to the
config adds it here.

It is REEF's 13 target species, the same list as
coral-gardeners-fish-detector@67c8627 resources/top25.yaml `little_cayman`
(pinned by a test).

The automatic track also offers the fish commonly recorded in the mission's
region (`with_regional`; the API's `regional_species`, 0085; the owner,
2026-10-30): with the targets alone, a fish that was none of them took the
nearest one's name -- mission 223's nurse shark became a 2.53 m "Red
Grouper". The Label Studio suggestions keep the targets alone: a labeler can
pick only what the config lists.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from typing import Sequence

from fishsense_services_contracts.species_prediction import SpeciesCandidate
from fishsense_services_contracts.taxonomy import parse_species_names
from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)

__all__ = [
    "FISH_BRANCH",
    "OTHER_CHOICE",
    "UNIDENTIFIABLE_CHOICE",
    "species_candidates",
    "with_regional",
]

FISH_BRANCH = "Fish"
#: The two "Fish" leaves that are no species: never a candidate. "Other" is
#: what a low-confidence prediction suggests instead (the open set).
UNIDENTIFIABLE_CHOICE = f"{FISH_BRANCH}, Unidentifiable (Cannot see)"
OTHER_CHOICE = f"{FISH_BRANCH}, Other (Identifiable but Nontarget)"
_NOT_SPECIES = frozenset({UNIDENTIFIABLE_CHOICE, OTHER_CHOICE})

#: The Taxonomy control the species answer is given in.
SPECIES_CONTROL = "species"


def species_candidates(
    xml: str = SPECIES_LABELING_CONFIG_XML,
) -> list[SpeciesCandidate]:
    """The config's "Fish" leaves, in its order, as BioCLIP's candidates."""
    root = ET.fromstring(xml)
    (taxonomy,) = [t for t in root.iter("Taxonomy") if t.get("name") == SPECIES_CONTROL]
    (fish,) = [c for c in taxonomy.findall("Choice") if c.get("value") == FISH_BRANCH]
    candidates = []
    for leaf in fish.findall("Choice"):
        choice = f"{FISH_BRANCH}, {leaf.get('value')}"
        if choice in _NOT_SPECIES:
            continue
        names = parse_species_names(choice)
        if names is None:
            raise ValueError(f"a Fish leaf that names no species: {choice!r}")
        candidates.append(SpeciesCandidate(choice=choice, scientific_name=names[1]))
    return candidates


def with_regional(regional: Sequence[tuple[str, str]]) -> list[SpeciesCandidate]:
    """The targets, then a region's (common name, scientific name) fish that
    are not targets, in its order, as "Fish, Common (Genus species)"."""
    targets = species_candidates()
    named = {c.scientific_name for c in targets}
    return targets + [
        SpeciesCandidate(
            choice=f"{FISH_BRANCH}, {common} ({name})", scientific_name=name
        )
        for common, name in regional
        if name not in named
    ]
