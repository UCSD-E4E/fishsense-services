"""The species predict activity: load BioCLIP, read the JPEG, classify the fish.

New in v2 (no v1 counterpart). The classifier is coral-gardeners-fish-detector
@67c8627's (bioclip_classifier.py), including its fallback: BioCLIP 2.5 first,
and BioCLIP 2 when 2.5 runs out of GPU memory, at load or at inference
(`load`, `_fallback_after_oom`). Built the way head/tail's predict activity is
(`headtail_predict.activities`):

* the weights are fetched and verified through fishsense-core
  (`species_predict.weights`), and a row records the verified weights' id;
* **a weights failure is non-retryable**, each cause its own type;
* the fallback stamps `SPECIES_FALLBACK_PREDICTOR_VERSION`, so its rows are
  permanently stale and re-predicted once the primary runs, and **a worker
  already on the fallback leaves an existing row alone**
  (`skipped_no_upgrade_available`, which the parent drops);
* the model loads once per process, and loading and inference run off the
  event loop.

v2 change from coral-gardeners: it fell back on any load error; here only an
out-of-memory does. A broken primary fails loudly instead of quietly
classifying every fish with the lesser model. The processor never touches the
database or Label Studio: it returns the result, and the orchestrator decides
what, if anything, a labeler is shown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fishsense_core.models import ModelIntegrityError, ModelUnavailable
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.species_prediction import (
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_FALLBACK_PREDICTOR_VERSION,
    SPECIES_PREDICTOR_VERSION,
    SPECIES_PRIMARY_MODEL_ID,
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    PredictSpeciesImageInput,
    SpeciesPredictionResult,
)
from fishsense_services_processor.species_predict.classifier import (
    BioclipClassifier,
    OpenClipEncoder,
    is_out_of_memory,
)
from fishsense_services_processor.species_predict.predict import predict_species

__all__ = ["SpeciesPredictActivities"]

#: Why BioCLIP's weights cannot load, by what the fetch raised: each its own
#: Temporal error type. The settings (`ValidationError`) are read on first
#: use, inside the fetch; `KeyError` is a manifest with no BioCLIP entry.
_WEIGHTS_FAILURES: tuple[tuple[type[BaseException], str, str], ...] = (
    (ValidationError, "BioclipSettingsInvalid",
     "FISHSENSE_BIOCLIP_* or FISHSENSE_MODEL_WEIGHTS_* is missing or invalid"),
    (ModelIntegrityError, "BioclipWeightsCorrupt",
     "the weights in model-weights are not the pinned file"),
    (ModelUnavailable, "BioclipWeightsUnavailable",
     "the weights are not in model-weights"),
    (KeyError, "BioclipNotInManifest", "the manifest has no BioCLIP entry"),
)  # fmt: skip

_TIERS = {
    SPECIES_PRIMARY_MODEL_ID: SPECIES_PREDICTOR_VERSION,
    SPECIES_FALLBACK_MODEL_ID: SPECIES_FALLBACK_PREDICTOR_VERSION,
}


def _release_gpu_memory() -> None:
    """coral-gardeners' `_cleanup_cuda`: best effort, and a no-op without
    torch or a GPU."""
    try:
        import torch  # pylint: disable=import-outside-toplevel,import-error

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # pylint: disable=broad-except
        pass


class SpeciesPredictActivities:
    """The predict activity, given how to reach the object store, how to get
    a model's verified weights (``model id -> (directory, weights id)``), and
    how to build an encoder from that directory."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        bioclip_weights: Callable[[str], Awaitable[tuple[Path, str]]],
        load_encoder: Callable[[Path], Any] = OpenClipEncoder.load,
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._bioclip_weights = bioclip_weights
        self._load_encoder = load_encoder
        self._classifier: BioclipClassifier | None = None
        self._lock = asyncio.Lock()

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    async def _verified_weights(self, model_id: str) -> tuple[Path, str]:
        try:
            return await self._bioclip_weights(model_id)
        except tuple(cause for cause, _, _ in _WEIGHTS_FAILURES) as exc:
            error_type, why = next(
                (t, w) for cause, t, w in _WEIGHTS_FAILURES if isinstance(exc, cause)
            )
            raise ApplicationError(
                f"BioCLIP's weights cannot load: {why}: {exc}",
                type=error_type,
                non_retryable=True,
            ) from exc

    async def _load(self, model_id: str) -> BioclipClassifier:
        directory, weights_id = await self._verified_weights(model_id)
        encoder = await asyncio.to_thread(self._load_encoder, directory)
        activity.logger.info("loaded %s (%s)", model_id, weights_id)
        return BioclipClassifier(
            encoder=encoder, model_id=weights_id, predictor_version=_TIERS[model_id]
        )

    async def _fall_back(self) -> BioclipClassifier:
        activity.logger.warning(
            "BioCLIP 2.5 ran out of GPU memory; falling back to BioCLIP 2"
        )
        self._classifier = None
        _release_gpu_memory()
        self._classifier = await self._load(SPECIES_FALLBACK_MODEL_ID)
        return self._classifier

    async def _loaded(self) -> BioclipClassifier:
        """The process's model: the primary, or the fallback once the primary
        ran out of memory. Loaded once; the lock is load-bearing, since a cold
        pod's first images arrive together."""
        async with self._lock:
            if self._classifier is None:
                try:
                    self._classifier = await self._load(SPECIES_PRIMARY_MODEL_ID)
                except Exception as exc:  # pylint: disable=broad-except
                    if not is_out_of_memory(exc):
                        raise
                    return await self._fall_back()
            return self._classifier

    async def _after_oom(self, failed: BioclipClassifier) -> BioclipClassifier:
        async with self._lock:
            if self._classifier is failed:
                return await self._fall_back()
            return self._classifier

    @activity.defn(name="predict_species_image")
    async def predict_species_image(
        self, payload: PredictSpeciesImageInput
    ) -> SpeciesPredictionResult:
        """Classify one fish, cropped from its head/tail JPEG by the box of
        the mask SAM 3.1 kept."""
        image = payload.image
        classifier = await self._loaded()
        on_fallback = classifier.predictor_version == SPECIES_FALLBACK_PREDICTOR_VERSION
        if on_fallback and image.has_existing_prediction:
            activity.logger.info(
                "skipping capture=%s: already predicted, and this worker is on "
                "the fallback",
                image.capture_id,
            )
            return SpeciesPredictionResult(
                capture_id=image.capture_id,
                headtail_prediction_id=image.headtail_prediction_id,
                status=SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
                predictor_version=SPECIES_FALLBACK_PREDICTOR_VERSION,
            )

        jpeg = await self._object_store().download_processed_jpeg(image.jpeg)
        try:
            result = await asyncio.to_thread(
                predict_species, jpeg, image, payload.candidates, classifier
            )
        except Exception as exc:  # pylint: disable=broad-except
            if on_fallback or not is_out_of_memory(exc):
                raise
            classifier = await self._after_oom(classifier)
            result = await asyncio.to_thread(
                predict_species, jpeg, image, payload.candidates, classifier
            )
        activity.logger.info(
            "predicted species capture=%s status=%s choice=%s p=%s margin=%s",
            result.capture_id,
            result.status,
            result.predicted_choice,
            (
                None
                if result.top1_probability is None
                else round(result.top1_probability, 3)
            ),
            None if result.margin is None else round(result.margin, 3),
        )
        return result
