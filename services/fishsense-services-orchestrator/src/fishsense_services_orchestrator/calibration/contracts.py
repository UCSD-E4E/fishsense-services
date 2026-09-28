"""What the calibration stages' orchestrator activities take and return.

Ported in shape from fishsense-lite@77e8f8e5: v1's stage-13 child took a bare
dive id and read and wrote through the SDK itself, its checkerboard resolver
returned `PerformCheckerboardCalibrationInput`, and its lattice study took
`VerifyCheckerboardLatticeParentInput {dive_ids, sample_limit=20}`.

v2 changes:

* a resolver returns the processor's payload **and the provenance** the
  result is recorded with -- the producer, the camera calibration, the slate
  template or board version, and the snapshot of the dive's labels the
  inputs were read at (`inputs_as_of`, which a refusal expires against);
* the processor's `LaserCalibrationResult` is recorded by the orchestrator
  (v1's data-worker PUT the extrinsics, or the refusal, itself);
* the lattice study names its tenant by slug and its dives by number (v1's
  dive ids; a migrated dive's number is its v1 id).

These stay inside the orchestrator, so they are not part of the processing
contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, PositiveInt

from fishsense_services_contracts.slate_calibration import (
    CheckerboardLatticeRender,
    LaserCalibrationResult,
    PerformCheckerboardCalibrationInput,
    SlateCalibrationInput,
    VerifyCheckerboardLatticeInput,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget

__all__ = [
    "CalibrationProvenance",
    "CheckerboardCalibrationPlan",
    "LatticeDive",
    "LatticeImport",
    "LatticePlan",
    "LatticeProject",
    "RecordCalibration",
    "SlateCalibrationPlan",
    "VerifyCheckerboardLatticeParentInput",
]


class CalibrationProvenance(BaseModel):
    """What a fit was computed from, recorded with it (PLAN.md §4.3)."""

    producer: Literal["slate", "checkerboard"]
    camera_calibration_id: uuid.UUID
    slate_template_id: uuid.UUID | None = None
    #: The *current* version of the dive's board, whose pitch the fit used.
    calibration_target_id: uuid.UUID | None = None
    #: The newest laser or slate label on the dive when the inputs were read,
    #: in Label Studio's clock. A refusal stands until a label is newer.
    inputs_as_of: datetime | None = None


class SlateCalibrationPlan(BaseModel):
    payload: SlateCalibrationInput
    provenance: CalibrationProvenance


class CheckerboardCalibrationPlan(BaseModel):
    payload: PerformCheckerboardCalibrationInput
    provenance: CalibrationProvenance


class RecordCalibration(BaseModel):
    """Append one attempt -- accepted or refused -- to a dive's calibrations."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID
    result: LaserCalibrationResult
    provenance: CalibrationProvenance


class VerifyCheckerboardLatticeParentInput(BaseModel):
    """Which calibrations to study, and how many frames of each (v1's).

    `dives` are the dives whose *own* calibration frames are rendered, by
    number. Include controls: a run holding only the suspect calibrations
    cannot tell "this detector mis-latticed these five" from "it
    mis-lattices everything". Twenty frames per calibration settles a
    systematic fault; None lifts the cap.
    """

    tenant: str
    dives: list[int]
    sample_limit: PositiveInt | None = 20


@dataclass(frozen=True)
class LatticeDive:
    tenant_id: uuid.UUID
    number: int
    sample_limit: int | None = 20


class LatticePlan(BaseModel):
    target: StagingTarget
    payload: VerifyCheckerboardLatticeInput


@dataclass(frozen=True)
class LatticeProject:
    """The study's one project for the tenant (v1: one project, full stop)."""

    tenant_id: uuid.UUID
    tenant_slug: str


class LatticeImport(BaseModel):
    tenant_id: uuid.UUID
    ls_project_id: int
    renders: list[CheckerboardLatticeRender]
