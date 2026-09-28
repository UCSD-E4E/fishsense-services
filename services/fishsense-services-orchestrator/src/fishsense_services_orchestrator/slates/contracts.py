"""What the slate stages' orchestrator activities take and return.

Ported in shape from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/reprocess_scope.py (`ClearReprocessFlagsInput`) and the arguments
v1's stage-9, populate and PDF-staging activities took (a dive id, a slate
id). v2 change: every one names its tenant, and dives are (tenant, dive)
`StagingTarget`s, so the object-store steps take them unchanged.

These stay inside the orchestrator -- the processor never sees them -- so they
are not part of the processing contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from fishsense_services_contracts.slate_calibration import PreprocessSlateImagesInput

__all__ = [
    "ClearSlateFlagsInput",
    "PopulateSlateProject",
    "SlatePdfTarget",
    "SlatePreprocessPlan",
]


class SlatePreprocessPlan(BaseModel):
    """Stage 9's resolved work: the processor's payload, and the checksums of
    the frames it redraws -- which is what the flags are cleared for after."""

    payload: PreprocessSlateImagesInput
    checksums: list[str]


@dataclass(frozen=True)
class ClearSlateFlagsInput:
    """A dive, and optionally the frames whose flags may come down (v1's
    `ClearReprocessFlagsInput`).

    `checksums=None` means the whole dive: the no-work backstop, since a flag
    that reached no image would otherwise hold the dive in its cohort forever.
    A list -- including an empty one -- means only these frames: the success
    path passes what it redrew, so a flag raised while the child ran (up to an
    hour) survives to the next firing.
    """

    tenant_id: uuid.UUID
    dive_id: uuid.UUID
    checksums: list[str] | None = None


@dataclass(frozen=True)
class SlatePdfTarget:
    """A slate template's PDF, staged per tenant."""

    tenant_id: uuid.UUID
    slate_template_id: uuid.UUID


@dataclass(frozen=True)
class PopulateSlateProject:
    """Push a dive's slate tasks into its project (v1: `(dive_id, project_id)`)."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID
    ls_project_id: int
