"""Workflow-input and report DTOs for dive ingestion.

Ported from fishsense-lite@a8b2c3bc libs/fishsense-shared/src/fishsense_shared/
ingest_contracts.py (the ingest half; the checksum-verification DTOs port with
their workflows). v2 changes, each pinned by a test:

* a request names its **tenant**;
* it refers to things the way an operator knows them -- a device **serial**, a
  slate **template name**, a calibration source's **NAS path** -- not v1 row ids;
* priorities are v2's lowercase ``high`` / ``low``;
* dive ids are v2 UUIDs.

One request means **one dive**. The images are the `.ORF` files directly inside
the named folder, not a recursive walk -- a dive has always been exactly one
directory (the legacy crawler assigned `dive = image.parent`), so recursing
would merge dives that are separate rows today. Subdirectories holding `.ORF`s
are reported so the operator can submit them separately, which is the Olympus
counter-rollover case.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Annotated

from pydantic import BaseModel, ConfigDict
from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "DuplicateOverlap",
    "IngestMissionRequest",
    "IngestPreflight",
    "IngestProgress",
    "IngestReport",
    "PreflightImage",
    "RejectedImage",
    "SubfolderReport",
]


class IngestMissionRequest(BaseModel):
    """What an operator submits. One request, one dive."""

    model_config = ConfigDict(validate_by_name=True)

    #: v2: the tenant the dive belongs to, by slug.
    tenant: str
    #: NAS path relative to the NAS raw root.
    mission_path: Annotated[str, renamed_from("dive_path", "mission_path")]
    #: Defaults to the leaf directory name. The dive name feeds Label Studio
    #: project titles, so it is worth getting right at ingest.
    mission_name: Annotated[str | None, renamed_from("dive_name", "mission_name")] = (
        None
    )

    #: Override device resolution, by serial. When unset the device is resolved
    #: from each frame's Olympus MakerNote serial; there is deliberately no
    #: fallback to EXIF `Artist`, because guessing a camera silently gives
    #: stage 14 the wrong intrinsics.
    device_serial: str | None = None

    #: High or the hourly cohorts never pick the dive up. Ingest still creates
    #: the dive at low and only flips it on success -- see `IngestReport`.
    priority: Literal["high", "low"] = "high"

    #: The dive's slate, by template name.
    slate_template: str | None = None

    #: A fish-only dive with no slate frames of its own can never self-calibrate,
    #: so stage 14 can never measure it. That is not detectable from the files,
    #: which is why intent must be stated: exactly one of these is required.
    #: v2: the source dive is named by its NAS path, within the same tenant.
    calibration_source_path: str | None = None
    self_calibrates: bool = False

    flip_mission_slate: Annotated[
        bool, renamed_from("flip_dive_slate", "flip_mission_slate")
    ] = False

    #: The camera port, when the operator knows it; recorded as theirs
    #: (``operator``, with ``port_note``). A device labelled with the ``D``
    #: suffix (``FSL-10D``, on the path or in every frame's Artist) is the
    #: corrective optic without being told, and a statement contradicting it
    #: fails preflight. Otherwise the port is ``unknown`` -- a missing ``D``
    #: is not evidence of a flat port (the owner's rules, 2026-10-16) -- and
    #: the stages that measure refuse it (PLAN.md §4.9).
    port: Literal["corrective_optic", "flat", "dome"] | None = None
    port_note: str | None = None
    #: What the mission is for. Stored only when stated; preflight suggests
    #: one from the name otherwise.
    purpose: (
        Literal[
            "survey",
            "lens_calibration",
            "laser_calibration",
            "slate",
            "known_length",
            "other",
        ]
        | None
    ) = None

    #: Preflight only: list, read EXIF headers, validate, write nothing.
    dry_run: bool = False

    # There is deliberately no `verify_existing` here. It existed as a declared
    # field honoured by no code, so setting it produced a normal ingest and no
    # warning -- worse than an absent flag. Re-hashing existing rows against the
    # NAS is the checksum-verification workflows' job, done read-only.


class PreflightImage(BaseModel):
    """One frame as seen by preflight, before anything is written."""

    path: str
    size: int
    #: None when the frame has no usable DateTime -- such a frame is rejected,
    #: never defaulted.
    taken_datetime: datetime | None = None
    #: The EXIF offset, recorded but not applied (see the tests).
    exif_offset: str | None = None
    serial_number: str | None = None
    artist: str | None = None


class SubfolderReport(BaseModel):
    """A subdirectory holding `.ORF`s: a separate dive under the existing
    convention (the Olympus counter-rollover case). Reported, never ingested."""

    path: str
    orf_count: int


class DuplicateOverlap(BaseModel):
    """How much of this folder already exists under another dive.

    Containment is `|new ∩ existing| / |new|` over content checksums -- a set
    operation, so it is immune to filenames and ordering and degrades to a
    partial overlap. It replaces the legacy whole-dive MD5 digest, which was
    all-or-nothing and basename-sensitive, and therefore wrong most times it
    was consulted on a corpus that is ~50% duplicates.
    """

    model_config = ConfigDict(validate_by_name=True)

    mission_id: Annotated[uuid.UUID, renamed_from("dive_id", "mission_id")]
    mission_path: Annotated[str, renamed_from("dive_path", "mission_path")]
    shared_images: int
    containment: float


class IngestPreflight(BaseModel):
    """Everything preflight found. Non-empty `errors` means nothing is written."""

    model_config = ConfigDict(validate_by_name=True)

    mission_path: Annotated[str, renamed_from("dive_path", "mission_path")]
    #: v2: resolved here, once, and handed to the later steps as ids.
    tenant_id: uuid.UUID | None = None
    resolved_calibration_source_mission_id: Annotated[
        uuid.UUID | None,
        renamed_from(
            "resolved_calibration_source_dive_id",
            "resolved_calibration_source_mission_id",
        ),
    ] = None
    resolved_slate_template_id: uuid.UUID | None = None
    images: List[PreflightImage] = []
    subfolders: List[SubfolderReport] = []
    #: v2: the tenant's device the frames resolve to.
    resolved_device_id: uuid.UUID | None = None
    resolved_device_name: str | None = None
    #: The mission's port and what said so (0047): ``device_suffix`` from a
    #: ``D`` label, ``operator`` from the request, else ``unknown``.
    resolved_port: str = "unknown"
    resolved_port_source: str | None = None
    resolved_port_note: str | None = None
    #: The request's purpose; never an inferred one.
    resolved_purpose: str | None = None
    total_bytes: int = 0
    #: Every problem at once -- one round trip for the operator, not a sequence.
    errors: List[str] = []
    warnings: List[str] = []
    duplicate_overlap: List[DuplicateOverlap] = []


class RejectedImage(BaseModel):
    """A frame the scan could not register, and why."""

    path: str
    reason: str


class IngestProgress(BaseModel):
    """Shape of the workflow's `progress` query."""

    model_config = ConfigDict(validate_by_name=True)

    state: str = "starting"
    mission_id: Annotated[uuid.UUID | None, renamed_from("dive_id", "mission_id")] = (
        None
    )
    total: int = 0
    scanned: int = 0
    registered: int = 0
    skipped_existing: int = 0
    rejected: int = 0
    current_path: str | None = None


class IngestReport(BaseModel):
    """The workflow's return value."""

    model_config = ConfigDict(validate_by_name=True)

    mission_path: Annotated[str, renamed_from("dive_path", "mission_path")]
    mission_id: Annotated[uuid.UUID | None, renamed_from("dive_id", "mission_id")] = (
        None
    )
    total: int = 0
    registered: int = 0
    skipped_existing: int = 0
    rejected: List[RejectedImage] = []
    #: MAX of the frames' timestamps, matching how every existing dive's
    #: datetime was derived.
    dive_datetime: datetime | None = None
    #: True only when every listed frame was persisted. This is what allows the
    #: dive to be flipped to high -- priority is the commit flag.
    committed: bool = False
    preflight: IngestPreflight | None = None
    duplicate_overlap: List[DuplicateOverlap] = []
