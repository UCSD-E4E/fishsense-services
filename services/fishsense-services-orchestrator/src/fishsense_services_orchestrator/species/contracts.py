"""What the species activities take and return, inside the orchestrator.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/reprocess_scope.py
(`ClearReprocessFlagsInput`) and update_dive_image_groups_activity.py
(`UpdateDiveImageGroupsResult`). v2 change: the target is (tenant, dive), and
the flag clear names captures rather than checksums (only a canonical capture
is ever drawn, so they are the same frames). These never cross to the
processor, so they are not part of the processing contract. A `SpeciesTarget`
serialises like the object store's `StagingTarget`, so a parent hands it to
the staging steps as it is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional

__all__ = ["ClearReprocessFlagsInput", "SpeciesTarget", "UpdateDiveImageGroupsResult"]


@dataclass(frozen=True)
class SpeciesTarget:
    tenant_id: uuid.UUID
    dive_id: uuid.UUID


@dataclass(frozen=True)
class ClearReprocessFlagsInput:
    """A dive, and optionally the frames whose flags may come down.

    `capture_ids=None` means the whole dive: the no-work backstop, where the
    flag reached no image, so nothing else will ever lower it. A list -- an
    empty one included -- means only these frames: the success path passes
    what it drew, so a flag raised while the child ran (up to two hours)
    survives to the next firing.
    """

    tenant_id: uuid.UUID
    dive_id: uuid.UUID
    capture_ids: Optional[List[uuid.UUID]] = None


@dataclass
class UpdateDiveImageGroupsResult:
    """Outcome of one stage-6.1 reconciliation for a single dive.

    `skipped_already_grouped` differentiates "no work to do" (label-studio
    clusters present) from "no work possible" (no prediction clusters or no
    species labels) — both report `new_clusters_created=0`, but the
    operator's response differs.
    """

    skipped_already_grouped: bool
    new_clusters_created: int
    species_labels_seen: int
