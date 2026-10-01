"""Which frames stage 2 draws, and where each sits in its cluster.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/resolve_species_preprocess_inputs_activity.py
(`_build_clusters`, `eligible`, the orphan block). Behaviour is v1's, and it
mirrors the stage-2 cohort exactly (a resolver that finds less than its
selector promised re-stages a dive hourly forever):

* canonical frames only -- the catalog hands over only canonical captures;
* a frame is eligible with a valid laser and no live species row in a Label
  Studio project, or with a flagged live row (the flag needs no laser: a
  laser superseded after flagging must not wedge the dive);
* "image i of N" is the frame's position in the WHOLE prediction cluster, not
  in the subset being drawn -- drawing 3 of 7 as 1/3..3/3 corrupts the keys
  their siblings' 4/7..7/7 share;
* an eligible frame in no cluster is its own 1 of 1, after the real clusters:
  stage 1 is one-shot per dive, so nothing else would ever draw it, and one
  undrawn frame keeps its dive's whole project an unpublished draft.

v2 change: the order of clusters and members is the catalog's stated one
(earliest capture first), where v1's was the database's incidental order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fishsense_services_api.species_store import (
    SpeciesCapture,
    SpeciesPreprocessFacts,
)

__all__ = ["PlannedMember", "plan_species_preprocess"]


@dataclass(frozen=True)
class PlannedMember:
    capture: SpeciesCapture
    cluster_index: int
    cluster_size: int


def plan_species_preprocess(facts: SpeciesPreprocessFacts) -> list[list[PlannedMember]]:
    """The frames to draw, grouped by cluster, orphans last as singletons."""
    by_id = {c.capture_id: c for c in facts.captures}
    labeled = {
        label.capture_id
        for label in facts.species_labels
        if label.ls_project_id is not None
    }
    flagged = {
        label.capture_id for label in facts.species_labels if label.needs_reprocess
    }

    def eligible(capture_id: uuid.UUID) -> bool:
        """Ordinary work, or a redraw the operator asked for."""
        return (
            capture_id in facts.valid_laser and capture_id not in labeled
        ) or capture_id in flagged

    groups: list[list[PlannedMember]] = []
    clustered: set[uuid.UUID] = set()
    for members in facts.prediction_clusters:
        size = len(members)
        selected = []
        for position, capture_id in enumerate(members, start=1):
            clustered.add(capture_id)
            if capture_id in by_id and eligible(capture_id):
                selected.append(PlannedMember(by_id[capture_id], position, size))
        if selected:
            groups.append(selected)

    groups.extend(
        [PlannedMember(capture, 1, 1)]
        for capture in facts.captures
        if capture.capture_id not in clustered and eligible(capture.capture_id)
    )
    return groups
