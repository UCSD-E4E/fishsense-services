"""Plan a regroup: stage 6.1's clusters in capture order, and which fish each
one is.

New in v2 (2026-10-14). The clusters are exactly stage 6.1's
(`species.grouping.regroup_by_species_labels`, which walks prediction
clusters in capture order), less what the store would drop anyway: a frame
that is not canonical, and a frame's second appearance. This module adds no
grouping rule. What it adds is the binding, because a new cluster with no fish
changes nothing that is already measured (migration 0028's stale-binding rule
compares a measurement with its cluster's fish, and an unbound cluster has
none).

Each frame votes for the fish it is now: its current measurements' fish, or,
with no current measurement, its label-studio cluster's fish (the highest
numbered, as `measurement_subjects` reads it). Counting the cluster matters:
a frame whose measurement is stale -- moved by an apply and not yet
re-measured, or on a dive recalibrated since -- is still that fish (PR #72
review: counting measurements alone planned those clusters unbound).

Fish are handed out greedily, largest vote first: ties go to the cluster
whose first voting frame is labeled the fish's species, then to the earlier
cluster, and each fish goes to at most one cluster. So a fish outvoted in
one cluster can end up with a cluster where it holds fewer frames; the moves
show it. A cluster with measured frames and no fish left gets a new fish of
its first measured frame's labeled species, as stage 14 would create it; any
other cluster is left unbound, for stage 14.

Every measured frame whose fish changes is a `Move`; stage 14 measures it
again under its new fish. A frame in a cluster left unbound does not move (an
unbound cluster invalidates nothing). The digest covers the clusters the plan
was made from and the clusters it writes, so an apply can insist on the plan
that was reviewed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from fishsense_services_api.regroup_store import NewCluster, RegroupFacts
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


def _groups(facts: RegroupFacts, labels, warnings: list[str]) -> list[list[uuid.UUID]]:
    """Stage 6.1's groups, as the store would write them: canonical frames
    only, each frame once."""
    seen: set[uuid.UUID] = set()
    groups = []
    for group in regroup_by_species_labels(facts.prediction_clusters, labels):
        kept = []
        for capture_id in group:
            if capture_id in seen:
                warnings.append(
                    f"frame {facts.capture_numbers[capture_id]} is in more than one "
                    f"prediction cluster; planned in the first group only"
                )
            elif capture_id in facts.canonical:
                kept.append(capture_id)
            seen.add(capture_id)
        if kept:
            groups.append(kept)
    return groups


def plan_regroup(facts: RegroupFacts) -> RegroupPlan:
    warnings: list[str] = []
    labels = select_species_label_per_image(facts.species_labels)
    groups = _groups(facts, labels, warnings)

    measured = defaultdict(list)
    for m in facts.measured:
        measured[m.capture_id].append(m)
    cluster_fish: dict[uuid.UUID, uuid.UUID] = {}
    for k in sorted(facts.label_studio_clusters, key=lambda k: k.number):
        for capture_id in k.members:
            if k.fish_id is None:
                cluster_fish.pop(capture_id, None)
            else:
                cluster_fish[capture_id] = k.fish_id

    def votes(capture_id: uuid.UUID) -> list[uuid.UUID]:
        if measured[capture_id]:
            return [m.fish_id for m in measured[capture_id]]
        bound = cluster_fish.get(capture_id)
        return [bound] if bound in facts.fish else []

    def names_of(capture_id: uuid.UUID | None) -> tuple[str, str] | None:
        if capture_id is None or capture_id not in labels:
            return None
        return parse_species_names(labels[capture_id].content_of_image)

    candidates = []
    for index, group in enumerate(groups):
        counts = Counter(f for c in group for f in votes(c))
        names = names_of(next((c for c in group if votes(c)), None))
        for fish_id, count in counts.items():
            same = names is not None and facts.fish[fish_id].scientific_name == names[1]
            candidates.append(
                (-count, not same, index, facts.fish[fish_id].number, fish_id)
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
        fish_id = owner.get(index)
        names = names_of(next((c for c in group if measured[c]), None))
        if fish_id is not None:
            clusters.append(NewCluster(list(group), fish_id=fish_id))
        elif names is not None:
            clusters.append(NewCluster(list(group), new_fish_names=names))
        else:
            clusters.append(NewCluster(list(group)))
            if any(measured[c] for c in group):
                warnings.append(
                    f"a group with measured frames has no species name, so it is "
                    f"left unbound and its frames keep their fish: "
                    f"{sorted(facts.capture_numbers[c] for c in group)}"
                )
            continue
        for capture_id in group:
            for m in measured[capture_id]:
                if m.fish_id != fish_id:
                    moves.append(
                        Move(
                            capture_number=m.capture_number,
                            from_fish=m.fish_number,
                            to_fish=None if fish_id is None
                            else facts.fish[fish_id].number,
                            to_species=None if fish_id is not None else names[1],
                            length_m=m.length_m,
                        )
                    )  # fmt: skip
        species = {labels[c].content_of_image for c in group if measured[c]}
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

    current = {
        (k.fish_id, frozenset(c for c in k.members if c in facts.canonical))
        for k in facts.label_studio_clusters
    }
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
