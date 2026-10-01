"""Head/tail (stage 5.1 preprocess and the SAM 3.1 predict): the contract.

Ported from fishsense-lite@77e8f8e5
libs/fishsense-shared/src/fishsense_shared/headtail_predictor.py (the version,
the crop, the Label Studio tag) and preprocess_contracts.py
(`PreprocessHeadtailImagesInput`, `PredictHeadtailImage`,
`PredictHeadtailImagesInput`, `HeadtailPredictionResult`,
`HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE`), plus the per-image
`PreprocessHeadtailImageInput` v1 kept in its data-worker. The version and the
tag live here because both sides read them: the processor stamps the version,
the orchestrator's cohort selects on a mismatch with it and tags Label Studio
predictions with it.

**Version the behaviour, not the model** (v1's rule). Bump
`HEADTAIL_PREDICTOR_VERSION` by hand whenever the stage's output would differ
for an unchanged image -- a different backend, crop, prompt, confidence band,
or *checkpoint release*. The cohort keys on a mismatch, so a bump drains as an
ordinary cohort. History: 1 (2026-09-03) the initial SAM3 stage, checkpoint
unrecorded; 2 (2026-09-07) `facebook/sam3.1`'s `sam3.1_multiplex.pt` pinned by
name. The fallback (fishsense-core's Mask R-CNN, when no GPU) stamps -1:
negative on purpose, so it can never equal a real version and a fallback row is
permanently stale -- that mismatch *is* the upgrade queue.

v2 changes:

* ids are v2's UUIDs (a capture, not v1's image id; laser labels by uuid);
* **the processor is handed ObjectRefs**: where to read the raw frame, and
  where to read or write the stage-5.1 JPEG. v1 handed it a checksum and a
  folder (`jpeg_folder`, `output_folder`) and let it build the key; in v2 only
  the orchestrator issues keys (PLAN.md §9.11);
* the workflow inputs carry their tenant (PLAN.md §9.4);
* `laser_label_ids` must parallel `laser_points` (v1 assumed it);
* a result carries its kept mask's box (`mask_bbox`, contract 5), which the
  species pre-annotation stage (`species_prediction`) crops by.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from fishsense_services_contracts._boxes import check_box
from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "HEADTAIL_CROP_HEIGHT",
    "HEADTAIL_CROP_WIDTH",
    "HEADTAIL_FALLBACK_PREDICTOR_VERSION",
    "HEADTAIL_PREDICTOR_VERSION",
    "HEADTAIL_STATUSES",
    "HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE",
    "HeadtailPredictionResult",
    "PredictHeadtailImage",
    "PredictHeadtailImagesInput",
    "PreprocessHeadtailImage",
    "PreprocessHeadtailImageInput",
    "PreprocessHeadtailImagesInput",
    "headtail_model_version_tag",
]

#: Bumped by hand when the stage's output would change for an unchanged image
#: (see the module docstring for the history).
HEADTAIL_PREDICTOR_VERSION = 2

#: Stamped by the Mask R-CNN fallback. Negative so it can never equal a real
#: version: a fallback row is stale until a GPU can upgrade it.
HEADTAIL_FALLBACK_PREDICTOR_VERSION = -1

#: The laser-centred crop, in rectified-frame pixels. Tuned on held-out frames
#: (v1's sweep); 1400-2200 is a plateau. Moving it bumps the version.
HEADTAIL_CROP_WIDTH = 1800
HEADTAIL_CROP_HEIGHT = 1350

#: The statuses a prediction row may carry: a prediction, or which kind of
#: abstention. An abstention is persisted too -- the cohort selects on a row's
#: absence, so an unrecorded one would be re-predicted every hour.
HEADTAIL_STATUSES = (
    "predicted",
    "no_detections",
    "laser_off_all_fish",
    "headtail_failed",
    "decode_failed",
)

#: What a GPU-less worker returns for an image it cannot improve on (a row
#: already exists and its laser is live). A statement about the worker, not the
#: image: the parent drops it, because persisting it would blank a good row.
HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE = "skipped_no_upgrade_available"


def headtail_model_version_tag(predictor_version: int | None = None) -> str:
    """The Label Studio `model_version` stamped on a pre-annotation.

    **Pass the row's own `predictor_version`**: the backfill dedupes on
    `(task_id, model_version)`, so tagging a fallback row as the current tier
    would make the later upgrade look already attached. It is an idempotency
    key, so it names the behaviour and nothing else -- never the checkpoint's
    path or the core version, which moved the key for identical output in v1.
    """
    version = (
        HEADTAIL_PREDICTOR_VERSION if predictor_version is None else predictor_version
    )
    return f"v{version} crop={HEADTAIL_CROP_WIDTH}x{HEADTAIL_CROP_HEIGHT}"


class PreprocessHeadtailImage(BaseModel):
    """One canonical capture to render: where its raw frame was staged, and
    where its stage-5.1 JPEG goes (over v1's for a migrated frame)."""

    capture_id: UUID
    #: Named back to the parent's reprocess-flag clear, which is scoped to the
    #: frames actually redrawn.
    checksum: str
    raw: ObjectRef
    jpeg: ObjectRef


class PreprocessHeadtailImagesInput(BaseModel):
    """Stage 5.1's workflow input (orchestrator -> processor)."""

    tenant_id: UUID
    dive_id: UUID
    images: List[PreprocessHeadtailImage]
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]


class PreprocessHeadtailImageInput(BaseModel):
    """What one `preprocess_headtail_image` activity renders."""

    raw: ObjectRef
    jpeg: ObjectRef
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]


class PredictHeadtailImage(BaseModel):
    """One image to predict: its stage-5.1 JPEG (the exact frame the labeler
    sees) and its live laser dots, which are both the gate and the crop
    centre. An image may carry more than one dot; first hit wins."""

    capture_id: UUID
    jpeg: ObjectRef
    laser_points: List[List[float]]
    #: Parallel to `laser_points`, so the result names the dot that chose the
    #: fish and a later supersede makes the row stale.
    laser_label_ids: List[UUID]
    #: A prediction row already exists, whatever tier wrote it. Only the
    #: activity knows whether it has a GPU, so only it can tell an upgrade
    #: from a repeat (or a downgrade).
    has_existing_prediction: bool = False
    #: ...unless the dot behind that row has since been superseded: the row
    #: may be of the wrong fish, and any backend fixes that.
    existing_laser_superseded: bool = False

    @model_validator(mode="after")
    def _parallel(self) -> "PredictHeadtailImage":
        if len(self.laser_label_ids) != len(self.laser_points):
            raise ValueError(
                "laser_label_ids must be parallel to laser_points "
                f"({len(self.laser_label_ids)} ids for {len(self.laser_points)})"
            )
        return self


class PredictHeadtailImagesInput(BaseModel):
    """The predict workflow's input (orchestrator -> processor). No
    intrinsics and no raw bytes: the stage reads the stage-5.1 JPEG."""

    tenant_id: UUID
    dive_id: UUID
    images: List[PredictHeadtailImage]


class HeadtailPredictionResult(BaseModel):
    """One image's prediction (processor -> orchestrator).

    Coordinates are rectified-frame pixels, already lifted out of the crop by
    `crop_x`/`crop_y`: the space the laser labels and a labeler's clicks are
    in. All four are None on an abstention, and `status` says which kind.
    """

    capture_id: UUID
    status: str
    head_x: Optional[float] = None
    head_y: Optional[float] = None
    tail_x: Optional[float] = None
    tail_y: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    mask_area_px: Optional[int] = None
    silhouette_ratio: Optional[float] = None
    crop_x: Optional[int] = None
    crop_y: Optional[int] = None
    #: The dot the answer came from: the one on the kept mask, or, with no
    #: mask kept, the first (the crop's centre). None only with no dot or no
    #: decodable frame. v1 set it on a prediction only.
    laser_label_id: Optional[UUID] = None
    predictor_version: Optional[int] = None
    checkpoint: Optional[str] = None
    core_version: Optional[str] = None
    #: v2, new (contract 5): the kept mask's box, ``[x_min, y_min, x_max,
    #: y_max)`` in rectified-frame pixels -- what the species stage crops by.
    #: None where no mask was kept, and from a processor older than the field.
    mask_bbox: Optional[List[int]] = None

    @field_validator("mask_bbox")
    @classmethod
    def _box(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        return check_box(value)
