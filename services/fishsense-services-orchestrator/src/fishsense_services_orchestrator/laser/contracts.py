"""What the laser slice's orchestrator activities and workflows exchange.

These stay inside the orchestrator: what crosses to the processor is
`fishsense_services_contracts.laser`. Ported in shape from fishsense-lite@
77e8f8e5 (`ClearReprocessFlagsInput` in activities/reprocess_scope.py and the
integer dive ids every laser activity took). v2 change: a target is a
(tenant, dive) pair; remediation also carries the dive's `number`, which is
what the operator's report names it by.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Annotated

from pydantic import BaseModel, Field, ConfigDict

from fishsense_services_contracts.laser import MissionPlan, PlanLaserRemediationInput
from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "ClearReprocessFlags",
    "MissionRemediation",
    "MissionRemediationRequest",
    "LaserTarget",
    "RemediationInputs",
    "RemediationTarget",
    "ReviveLabels",
]


class LaserTarget(BaseModel):
    """A dive of a tenant. Serializes like `object_store.StagingTarget`, so it
    is what the staging and cleanup steps take too."""

    model_config = ConfigDict(validate_by_name=True)

    tenant_id: uuid.UUID
    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]


class ClearReprocessFlags(BaseModel):
    """v1's `ClearReprocessFlagsInput`: `capture_ids` None clears the whole dive
    (the no-work backstop), [] clears nothing, a list scopes the clear to the
    frames redrawn (v1: by checksum)."""

    target: LaserTarget
    capture_ids: Optional[List[uuid.UUID]] = None


class RemediationTarget(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    tenant_id: uuid.UUID
    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]
    #: v1's dive id for a migrated dive: the report's name for it.
    number: int


class MissionRemediationRequest(BaseModel):
    """Plan one dive -- and, with `revive_ids`, apply a reviewed plan to it."""

    model_config = ConfigDict(validate_by_name=True)

    target: RemediationTarget
    excluded_label_ids: List[int] = Field(default_factory=list)
    mission_excluded: Annotated[
        bool, renamed_from("dive_excluded", "mission_excluded")
    ] = False
    revive_ids: List[int] = Field(default_factory=list)


class RemediationInputs(BaseModel):
    """The processor's plan input, the digest of exactly the rows it was made
    from, and which of the requested ids were already live."""

    plan_input: PlanLaserRemediationInput
    fingerprint: str


class ReviveLabels(BaseModel):
    target: RemediationTarget
    #: The reviewed ids still superseded when the plan was re-made.
    pending: List[int]
    #: What the fresh plan would revive.
    planned: List[int]
    fingerprint: str


class MissionRemediation(BaseModel):
    plan: MissionPlan
    written: Optional[int] = None
