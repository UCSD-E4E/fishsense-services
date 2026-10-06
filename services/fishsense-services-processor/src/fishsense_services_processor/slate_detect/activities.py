"""The slate detect activity: load the classifier, render the raw, score it.

New in v2. The model is 2026-10-03_slate_detector@95a77d95's presence
classifier (`slate_detect.model`), over the frame it was trained on
(`slate_detect.frame`). Built the way the species and head/tail predict
activities are:

* the weights are fetched and verified through fishsense-core
  (`slate_detect.weights`), and every result records, for publication, the
  model's name and `SLATE_DETECTOR_VERSION`, the verified weights' sha256,
  fishsense-core's and the processor's installed versions, the render
  (`frame.render_settings`) and when it was scored;
* **a weights, settings or checkpoint failure is non-retryable**, each cause
  its own type: retrying re-downloads the same refused bytes per frame;
* the classifier loads once per process, under a lock (a cold pod's first
  frames arrive together), and the decode and inference run off the event
  loop;
* a raw that will not decode is an abstention (`decode_failed`), returned so
  the orchestrator records it and the cohort moves on; any other failure is
  raised, for Temporal to retry.

The processor never touches the database or Label Studio: it returns the
result, and the orchestrator decides what it feeds.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fishsense_core.models import ModelIntegrityError, ModelUnavailable
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.slate_presence import (
    SLATE_DETECTOR_MODEL_NAME,
    SLATE_DETECTOR_VERSION,
    DetectSlateImageInput,
    SlatePresenceResult,
)
from fishsense_services_processor.slate_detect.frame import (
    DECODE_ERRORS,
    render_frame,
    render_settings,
)

__all__ = ["CheckpointInvalid", "SlateDetectActivities"]


class CheckpointInvalid(ValueError):
    """The verified file is not a checkpoint this stage can load. The model
    module's own error is translated to this one, so the activity can name it
    without importing torch."""


#: Why the weights cannot load, by what the fetch raised: each its own
#: Temporal error type. The settings (`ValidationError`) are read on first
#: use, inside the fetch; `KeyError` is a manifest with no entry.
_WEIGHTS_FAILURES: tuple[tuple[type[BaseException], str, str], ...] = (
    (ValidationError, "SlateDetectorSettingsInvalid",
     "FISHSENSE_SLATE_DETECTOR_* or FISHSENSE_MODEL_WEIGHTS_* is missing or invalid"),
    (ModelIntegrityError, "SlateDetectorWeightsCorrupt",
     "the weights in model-weights are not the pinned file"),
    (ModelUnavailable, "SlateDetectorWeightsUnavailable",
     "the weights are not in model-weights"),
    (KeyError, "SlateDetectorNotInManifest", "the manifest has no slate-detector entry"),
)  # fmt: skip


def _installed(package: str) -> str | None:
    """An installed package's version: provenance only (the processor's is
    its image's release)."""
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _load_classifier(path: Path, weights_sha256: str) -> Any:
    """The torch classifier; imported here, since only the GPU image has
    torch."""
    # pylint: disable=import-outside-toplevel
    from fishsense_services_processor.slate_detect import model

    try:
        return model.SlateClassifier.load(path, weights_sha256)
    except model.CheckpointInvalid as exc:
        raise CheckpointInvalid(str(exc)) from exc


class SlateDetectActivities:
    """The detect activity, given how to reach the object store, how to get
    the verified weights (``() -> (path, sha256)``), how to load a classifier
    from them, and how to render a raw."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        weights: Callable[[], Awaitable[tuple[Path, str]]],
        load_classifier: Callable[[Path, str], Any] = _load_classifier,
        render: Callable[..., Any] = render_frame,
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._weights = weights
        self._load_classifier = load_classifier
        self._render = render
        self._classifier: Any = None
        self._sha256: str | None = None
        self._lock = asyncio.Lock()

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    async def _verified_weights(self) -> tuple[Path, str]:
        try:
            return await self._weights()
        except tuple(cause for cause, _, _ in _WEIGHTS_FAILURES) as exc:
            error_type, why = next(
                (t, w) for cause, t, w in _WEIGHTS_FAILURES if isinstance(exc, cause)
            )
            raise ApplicationError(
                f"the slate detector's weights cannot load: {why}: {exc}",
                type=error_type,
                non_retryable=True,
            ) from exc

    async def _loaded(self) -> Any:
        async with self._lock:
            if self._classifier is None:
                path, sha256 = await self._verified_weights()
                try:
                    self._classifier = await asyncio.to_thread(
                        self._load_classifier, path, sha256
                    )
                except CheckpointInvalid as exc:
                    raise ApplicationError(
                        f"the slate detector's checkpoint cannot load: {exc}",
                        type="SlateDetectorCheckpointInvalid",
                        non_retryable=True,
                    ) from exc
                self._sha256 = sha256
                activity.logger.info("loaded the slate detector (sha256=%s)", sha256)
            return self._classifier

    @activity.defn(name="detect_slate_presence")
    async def detect_slate_presence(
        self, payload: DetectSlateImageInput
    ) -> SlatePresenceResult:
        """P(slate) for one staged raw frame."""
        classifier = await self._loaded()
        image = payload.image
        common = {
            "capture_id": image.capture_id,
            "model_name": SLATE_DETECTOR_MODEL_NAME,
            "model_version": SLATE_DETECTOR_VERSION,
            "weights_sha256": self._sha256,
            "core_version": _installed("fishsense-core"),
            "processor_version": _installed("fishsense-services-processor"),
            "render": render_settings(),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = await self._object_store().download_raw(image.raw, Path(tmpdir))
            try:
                frame = await asyncio.to_thread(
                    self._render,
                    raw,
                    payload.camera_matrix,
                    payload.distortion_coefficients,
                )
            except DECODE_ERRORS as exc:
                activity.logger.warning(
                    "raw would not decode capture=%s: %s", image.capture_id, exc
                )
                return SlatePresenceResult(
                    status="decode_failed", predicted_at=_now(), **common
                )
        probability = await asyncio.to_thread(classifier.probability, frame)
        activity.logger.info(
            "slate presence capture=%s p=%.4f", image.capture_id, probability
        )
        return SlatePresenceResult(
            status="predicted",
            probability=probability,
            predicted_at=_now(),
            **common,
        )
