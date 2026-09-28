"""Stage 6.1: labelers' grouping choices regroup the prediction clusters.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/update_dive_image_groups_activity.py
(`select_species_label_per_image`, `regroup_by_species_labels`), itself the
port of scripts/stage6.1_update_dive_image_groups.ipynb. Behaviour is v1's:

* `grouping == "Part of previous group"` keeps appending to the current
  group, even across prediction-cluster boundaries;
* `grouping == "Not part of current group"` flushes the current group and
  starts a new one with that frame;
* the first frame of each prediction cluster otherwise starts a new group;
* a frame with no labeler's answer (only a sentinel, or superseded rows) is in
  no group -- it must not be measured, and must not start a group (prod dive
  5: a sentinel split one hogfish into two fish).

The order of clusters and members is load-bearing ("previous" crosses
cluster boundaries). v2 change: it is the catalog's stated order (earliest
capture first), where v1's was the database's incidental one; and a tie
between two real rows breaks on the higher `number` (v1: the higher id -- the
most recently written).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fishsense_services_api.species_store import SpeciesLabelRow

__all__ = [
    "GROUPING_BREAK",
    "GROUPING_CONTINUE",
    "regroup_by_species_labels",
    "select_species_label_per_image",
]

GROUPING_CONTINUE = "Part of previous group"
GROUPING_BREAK = "Not part of current group"


def select_species_label_per_image(
    species_labels: Iterable[SpeciesLabelRow],
) -> dict[uuid.UUID, SpeciesLabelRow]:
    """One species label per frame — the labeler's answer, and only that.

    **A sentinel is not an answer**, and neither is a superseded row. Where a
    frame holds rows in two real projects (its per-dive project plus a
    grandfathered one), the most recently written wins, as a stated rule
    rather than an accident of row order.
    """
    chosen: dict[uuid.UUID, SpeciesLabelRow] = {}
    for label in species_labels:
        if label.ls_project_id is None or label.superseded:
            continue
        current = chosen.get(label.capture_id)
        if current is None or label.number >= current.number:
            chosen[label.capture_id] = label
    return chosen


def regroup_by_species_labels(
    prediction_clusters: Iterable[list[uuid.UUID]],
    species_label_by_capture: dict[uuid.UUID, SpeciesLabelRow],
) -> list[list[uuid.UUID]]:
    """Apply labelers' grouping choices to the prediction clusters' captures.

    Returns one list of captures per label-studio cluster, in the order the
    prediction clusters were visited. Frames without a chosen label are
    skipped (their grouping is unknown).
    """
    groups: list[list[uuid.UUID]] = []
    current: list[uuid.UUID] = []
    for cluster in prediction_clusters:
        for idx, capture_id in enumerate(cluster):
            label = species_label_by_capture.get(capture_id)
            if label is None:
                continue
            starts_new_group = (
                idx == 0 and label.grouping != GROUPING_CONTINUE
            ) or label.grouping == GROUPING_BREAK
            if starts_new_group and current:
                groups.append(current)
                current = []
            current.append(capture_id)
    if current:
        groups.append(current)
    return groups
