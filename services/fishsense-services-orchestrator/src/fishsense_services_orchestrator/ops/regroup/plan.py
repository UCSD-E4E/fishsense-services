"""Plan a regroup: stage 6.1's clusters in capture order, and which fish each
one is.

New in v2 (2026-10-14). The clusters are exactly stage 6.1's
(`species.grouping.regroup_by_species_labels`, which walks prediction
clusters in capture order); this module adds no grouping rule. What it adds
is the binding, because a new cluster with no fish changes nothing that is
already measured (migration 0028's stale-binding rule compares a measurement
with its cluster's fish, and an unbound cluster has none):

* a fish is kept by the new cluster holding most of its measured frames --
  ties go to the cluster whose first measured frame is labelled the fish's
  species, then to the earlier cluster -- and each fish goes to at most one;
* a cluster with measured frames and no fish left gets a new fish of the
  species its first measured frame is labelled, as stage 14 would create it;
* a cluster with no measured frame is left unbound, for stage 14.

Every measured frame whose fish changes is a `Move`; stage 14 measures it
again under its new fish. The digest covers the clusters the plan was made
from and the clusters it writes, so an apply can insist on the plan that was
reviewed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from fishsense_services_api.regroup_store import (
    MeasuredCapture,
    NewCluster,
    RegroupFacts,
)
from fishsense_services_contracts.taxonomy import parse_species_names
from fishsense_services_orchestrator.species.grouping import (
    regroup_by_species_labels,
    select_species_label_per_image,
)

__all__ = ["Move", "RegroupPlan", "plan_regroup"]


@dataclass(frozen=True)
class Move:
    """A measured frame whose fish changes."""

    capture_number: int
    from_fish: int
    #: The fish's number; None for a new fish.
    to_fish: int | None
    #: The new fish's scientific name (None when it is an existing fish).
    to_species: str | None
    length_m: float


@dataclass(frozen=True)
class RegroupPlan:
    clusters: list[NewCluster]
    moves: list[Move]
    #: The dive is already grouped exactly this way.
    noop: bool
    sha256: str
    warnings: list[str] = field(default_factory=list)


def _digest(facts: RegroupFacts, clusters: list[NewCluster]) -> str:
    planned = [
        [
            [str(c) for c in k.captures],
            str(k.fish_id or ""),
            list(k.new_fish_names or ()),
        ]
        for k in clusters
    ]
    payload = json.dumps({"from": facts.fingerprint, "to": planned})
    return hashlib.sha256(payload.encode()).hexdigest()


def plan_regroup(facts: RegroupFacts) -> RegroupPlan:
    labels = select_species_label_per_image(facts.species_labels)
    groups = regroup_by_species_labels(facts.prediction_clusters, labels)
    measured: dict[uuid.UUID, list[MeasuredCapture]] = defaultdict(list)
    for m in facts.measured:
        measured[m.capture_id].append(m)
    fish_numbers = {m.fish_id: m.fish_number for m in facts.measured}
    fish_species = {m.fish_id: m.fish_scientific_name for m in facts.measured}
    warnings: list[str] = []

    # The species each group's first measured frame is labelled.
    group_names: list[tuple[str, str] | None] = []
    for group in groups:
        first = next((c for c in group if measured[c]), None)
        group_names.append(
            None
            if first is None
            else parse_species_names(labels[first].content_of_image)
        )

    candidates = []
    for index, group in enumerate(groups):
        counts = Counter(m.fish_id for c in group for m in measured[c])
        names = group_names[index]
        for fish_id, count in counts.items():
            same_species = names is not None and fish_species[fish_id] == names[1]
            candidates.append(
                (-count, not same_species, index, fish_numbers[fish_id], fish_id)
            )
    owner: dict[int, uuid.UUID] = {}
    taken: set[uuid.UUID] = set()
    for _, _, index, _, fish_id in sorted(candidates):
        if index not in owner and fish_id not in taken:
            owner[index] = fish_id
            taken.add(fish_id)

    clusters: list[NewCluster] = []
    moves: list[Move] = []
    for index, group in enumerate(groups):
        names = group_names[index]
        fish_id = owner.get(index)
        if fish_id is not None:
            clusters.append(NewCluster(list(group), fish_id=fish_id))
        elif names is not None:
            clusters.append(NewCluster(list(group), new_fish_names=names))
        else:
            clusters.append(NewCluster(list(group)))
            if any(measured[c] for c in group):
                warnings.append(
                    f"a group with measured frames has no species name: "
                    f"{sorted(facts.capture_numbers[c] for c in group)}"
                )
        for capture_id in group:
            for m in measured[capture_id]:
                if fish_id is not None and m.fish_id == fish_id:
                    continue
                moves.append(
                    Move(
                        capture_number=m.capture_number,
                        from_fish=m.fish_number,
                        to_fish=None if fish_id is None else fish_numbers[fish_id],
                        to_species=(
                            None if fish_id is not None or names is None else names[1]
                        ),
                        length_m=m.length_m,
                    )
                )
        species = {
            labels[c].content_of_image for c in group if measured[c] and c in labels
        }
        if len(species) > 1:
            warnings.append(
                f"a group's measured frames carry {len(species)} species labels: "
                f"{sorted(facts.capture_numbers[c] for c in group)}"
            )

    grouped = {c for group in groups for c in group}
    for m in facts.measured:
        if m.capture_id not in grouped:
            warnings.append(
                f"measured frame {m.capture_number} (fish {m.fish_number}) is in no "
                f"group; its measurement stays as it is"
            )
    if facts.priority != "high":
        warnings.append(
            f"dive priority is {facts.priority!r}: stage 14 measures only 'high' "
            f"dives, so moved frames will not be re-measured until it is raised"
        )

    current = {(k.fish_id, frozenset(k.members)) for k in facts.label_studio_clusters}
    planned = {(k.fish_id, frozenset(k.captures)) for k in clusters}
    noop = (
        len(facts.label_studio_clusters) == len(clusters)
        and current == planned
        and all(k.new_fish_names is None for k in clusters)
    )
    return RegroupPlan(
        clusters=clusters,
        moves=moves,
        noop=noop,
        sha256=_digest(facts, clusters),
        warnings=warnings,
    )
