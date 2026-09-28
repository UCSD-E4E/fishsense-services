"""BioCLIP species pre-annotation: the contract.

**New in v2; v1 has no counterpart.** The classifier is ported from
coral-gardeners-fish-detector@67c8627:
src/coral_fish_pipeline/classification/bioclip_classifier.py (the models, the
closed-set softmax, top-5 and the margin), species_prompts.py (the four prompt
templates) and configs/default.yaml's `crop:` block (20% padding, a 64 px
floor). The constants live here because both sides read them: the processor
builds the prompts and the crop and stamps the version; the orchestrator's
cohort selects on a mismatch with it.

**Pre-annotation only.** A prediction is shown to a labeler as a suggestion
in the species Label Studio task, and a human confirms every label. Nothing
here, or downstream of it, writes or completes a species label.

The fish classified is the one the head/tail stage already segments with SAM
3.1 at the laser dot: its kept mask's box (`HeadtailPredictionResult.
mask_bbox`), padded, cropped from the head/tail stage's rendered JPEG. So
there is no second SAM pass, and this stage re-runs independently of it.

**Version the behaviour, not the model** (head/tail's rule). Bump
`SPECIES_PREDICTOR_VERSION` by hand whenever the output would differ for an
unchanged crop: another model or checkpoint, prompt set, crop or scoring.
History: 1 (2026-09-28) BioCLIP 2.5 ViT-H/14 over the four templates below,
mean-normalised text embeddings, logit-scaled softmax over the species
labeling config's target leaves. The fallback (BioCLIP 2, when 2.5 runs out of
GPU memory) stamps -1: negative on purpose, so a fallback row is permanently
stale and re-predicted once the primary can run.
"""

from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

from fishsense_services_contracts._boxes import check_box
from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "SPECIES_CROP_MIN_SIZE",
    "SPECIES_CROP_PADDING",
    "SPECIES_FALLBACK_MODEL_ID",
    "SPECIES_FALLBACK_PREDICTOR_VERSION",
    "SPECIES_PREDICTOR_VERSION",
    "SPECIES_PRIMARY_MODEL_ID",
    "SPECIES_PROMPT_TEMPLATES",
    "SPECIES_STATUSES",
    "SPECIES_STATUS_NO_UPGRADE_AVAILABLE",
    "SPECIES_TOP_K",
    "PredictSpeciesImage",
    "PredictSpeciesImageInput",
    "PredictSpeciesImagesInput",
    "SpeciesCandidate",
    "SpeciesPredictionResult",
    "SpeciesScore",
    "species_prompts",
]

#: Bumped by hand when the output would change for an unchanged crop (see the
#: module docstring for the history).
SPECIES_PREDICTOR_VERSION = 1

#: Stamped by the BioCLIP 2 fallback. Negative so it never equals a real
#: version: a fallback row is stale until the primary can run.
SPECIES_FALLBACK_PREDICTOR_VERSION = -1

#: coral-gardeners' pair. The original BioCLIP is forbidden (its rule); the
#: processor refuses any other id.
SPECIES_PRIMARY_MODEL_ID = "hf-hub:imageomics/bioclip-2.5-vith14"
SPECIES_FALLBACK_MODEL_ID = "hf-hub:imageomics/bioclip-2"

#: coral-gardeners' species_prompts.py, verbatim. Each species' text embedding
#: is the normalised mean of these four.
SPECIES_PROMPT_TEMPLATES = (
    "a photo of a {name}",
    "an underwater photo of a {name}",
    "a reef fish species {name}",
    "{name}, a reef fish",
)

#: coral-gardeners' configs/default.yaml `crop:` (its cropper defaults to
#: 0.30; the pipeline runs 0.20): the box grows by this fraction of its size
#: on every side, and never below the floor, then is clipped to the frame.
SPECIES_CROP_PADDING = 0.20
SPECIES_CROP_MIN_SIZE = 64

#: How many ranked choices a prediction records.
SPECIES_TOP_K = 5

#: What a row may record: a prediction, or a JPEG that would not decode (an
#: abstention is persisted too: the cohort selects on a row's absence).
SPECIES_STATUSES = ("predicted", "decode_failed")

#: What a worker already on the fallback returns for a capture that has a row:
#: it cannot improve on it. The parent drops it (head/tail's rule).
SPECIES_STATUS_NO_UPGRADE_AVAILABLE = "skipped_no_upgrade_available"


def species_prompts(scientific_name: str) -> list[str]:
    """The four prompts for one species (coral-gardeners' `prompts_for_species`)."""
    return [
        template.format(name=scientific_name) for template in SPECIES_PROMPT_TEMPLATES
    ]


class SpeciesCandidate(BaseModel):
    """One choice BioCLIP may give: the full species taxonomy value a labeler
    would pick, and the scientific name the prompts are built from."""

    choice: str
    scientific_name: str


class SpeciesScore(BaseModel):
    choice: str
    probability: float


class PredictSpeciesImage(BaseModel):
    """One fish to classify: the head/tail stage's JPEG (the rectified frame),
    and the box of the mask it kept at the laser dot."""

    capture_id: UUID
    #: The head/tail prediction the box is from. A newer one makes the species
    #: row stale.
    headtail_prediction_id: UUID
    jpeg: ObjectRef
    #: ``[x_min, y_min, x_max, y_max)`` in the JPEG's pixels.
    mask_bbox: List[int]
    #: A species row already exists. Only the activity knows whether it is
    #: running the fallback, so only it can tell a repeat from an upgrade.
    has_existing_prediction: bool = False

    @field_validator("mask_bbox")
    @classmethod
    def _box(cls, value: List[int]) -> List[int]:
        return check_box(value)


def _candidates(value: List[SpeciesCandidate]) -> List[SpeciesCandidate]:
    if not value:
        raise ValueError("at least one candidate is needed")
    choices = [c.choice for c in value]
    if len(set(choices)) != len(choices):
        raise ValueError(f"candidate choices repeat: {choices}")
    return value


class PredictSpeciesImagesInput(BaseModel):
    """The predict workflow's input (orchestrator -> processor). The
    candidates come from the species labeling config, which the orchestrator
    owns; the processor never reads it."""

    tenant_id: UUID
    dive_id: UUID
    candidates: List[SpeciesCandidate]
    images: List[PredictSpeciesImage]

    @field_validator("candidates")
    @classmethod
    def _checked(cls, value: List[SpeciesCandidate]) -> List[SpeciesCandidate]:
        return _candidates(value)


class PredictSpeciesImageInput(BaseModel):
    """What one `predict_species_image` activity classifies."""

    image: PredictSpeciesImage
    candidates: List[SpeciesCandidate]

    @field_validator("candidates")
    @classmethod
    def _checked(cls, value: List[SpeciesCandidate]) -> List[SpeciesCandidate]:
        return _candidates(value)


class SpeciesPredictionResult(BaseModel):
    """One fish's prediction (processor -> orchestrator).

    `predicted_choice` is BioCLIP's top-1 over the closed set. Whether it is
    suggested, or "Other" instead, is decided when a task is seeded, so the
    threshold can be retuned without re-predicting. All the scores are None
    on an abstention.
    """

    capture_id: UUID
    headtail_prediction_id: UUID
    status: str
    predicted_choice: Optional[str] = None
    top1_probability: Optional[float] = None
    #: top-1 minus top-2 probability.
    margin: Optional[float] = None
    top5: List[SpeciesScore] = []
    predictor_version: Optional[int] = None
    #: The verified weights (``bioclip/2.5-vith14@<sha256[:12]>``).
    model_id: Optional[str] = None
