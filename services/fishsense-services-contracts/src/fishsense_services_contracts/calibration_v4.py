"""Calibration v4's slate pose (v18, 2026-10-23): each slate frame's pose
found automatically on the GPU -- SAM's slate mask, the board fitted to it,
the template's printed pattern aligned to the board -- and the automatic dot's
point on that plane, for the read-only replay to fit a beam through. New
models only; production's calibration is v2 until the owner switches it."""

from __future__ import annotations

from typing import Annotated, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renamed import renamed_from

__all__ = ["POSE_ECC_MIN", "SlatePose", "SlatePoseFrame", "SlatePoseFramesInput"]

#: The pattern alignment's correlation below which a frame is not posed
#: (``weak_alignment``): pool frames aligned at 0.8-1.0 put the reference
#: points a median 1.9-2.1 px from the human clicks (1600-px cache,
#: 2026-10-23). Provisional: on a synthetic slate the correlation falls with
#: image noise alone (0.79 -> 0.53) while the points stay within 1 px, so it
#: may refuse good reef frames; every pose's point is reported whatever its
#: correlation, for the threshold to be set from the replay.
POSE_ECC_MIN = 0.8


class SlatePoseFrame(BaseModel):
    """A slate frame: its render (presigned GET) and its automatic dot
    (full-resolution render px)."""

    capture_id: UUID
    render_url: str
    dot: List[float]


class SlatePoseFramesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    #: The renders' (full-resolution) camera matrix.
    camera_matrix: List[List[float]]
    #: The mission's slate template: its staged PDF, the DPI its reference
    #: points are in, and those points (page px).
    slate_pdf: ObjectRef
    slate_dpi: int
    reference_points: List[List[float]]
    frames: List[SlatePoseFrame]


class SlatePose(BaseModel):
    """One frame's pose, or how far it got. `status`: ``posed``,
    ``weak_alignment`` (posed, `ecc` under `POSE_ECC_MIN`: not used),
    ``no_mask``, ``no_board``, ``not_aligned``, ``no_plane``, ``no_point``,
    ``render_unavailable``, ``decode_failed``."""

    capture_id: UUID
    status: str
    sam_prompt: Optional[str] = None
    sam_score: Optional[float] = None
    board_covered: Optional[float] = None
    board_extra: Optional[float] = None
    tilt_deg: Optional[float] = None
    ecc: Optional[float] = None
    #: How far a rigid board's pose misses the aligned reference points.
    pnp_rms_px: Optional[float] = None
    #: The reference points as aligned (full-resolution px), for review.
    reference_image_points: List[List[float]] = []
    #: The dot's point on the slate (camera space, m).
    point: Optional[List[float]] = None
    #: The other way up's correlation (v23): the margin of the orientation
    #: chosen; None if it did not converge.
    ecc_other: Optional[float] = None
    #: The direction of the page's top in the image (v23), degrees, 0 = up,
    #: clockwise positive: the orientation in the world.
    page_up_deg: Optional[float] = None
    #: The dot on the rigid board's plane, fitted to SAM's mask (v24; camera
    #: space, m): a range no orientation of the page decides.
    board_point: Optional[List[float]] = None
