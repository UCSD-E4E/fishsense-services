"""One species prediction: crop the fish, classify it, record the answer.

New in v2 (no v1 counterpart); the scoring is coral-gardeners-fish-detector@
67c8627's (see `classifier`), the crop its cropper's (see `crop`). No
threshold is applied here: the result is BioCLIP's top-1 over the closed set,
and the orchestrator decides at seed time whether to suggest it or "Other",
so the threshold can be retuned against rows already collected.
"""

from __future__ import annotations

from typing import Any, Sequence

from fishsense_services_contracts.species_prediction import (
    PredictSpeciesImage,
    SpeciesCandidate,
    SpeciesPredictionResult,
    SpeciesScore,
)
from fishsense_services_processor.species_predict.crop import crop_fish

__all__ = ["predict_species"]


def predict_species(
    jpeg_bytes: bytes,
    image: PredictSpeciesImage,
    candidates: Sequence[SpeciesCandidate],
    classifier: Any,
) -> SpeciesPredictionResult:
    """`classifier` is anything with `classify(image, candidates) -> Ranking`,
    `model_id` and `predictor_version`: production a `BioclipClassifier`,
    tests one over a fake encoder."""
    common = {
        "capture_id": image.capture_id,
        "headtail_prediction_id": image.headtail_prediction_id,
        "predictor_version": classifier.predictor_version,
        "model_id": classifier.model_id,
    }
    crop = crop_fish(jpeg_bytes, image.mask_bbox)
    if crop is None:
        return SpeciesPredictionResult(status="decode_failed", **common)
    ranking = classifier.classify(crop, candidates)
    return SpeciesPredictionResult(
        status="predicted",
        predicted_choice=ranking.top5[0][0],
        top1_probability=ranking.top1_probability,
        margin=ranking.margin,
        top5=[SpeciesScore(choice=c, probability=p) for c, p in ranking.top5],
        **common,
    )
