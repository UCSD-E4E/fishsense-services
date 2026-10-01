"""Model-assisted laser labeling (processor, GPU role).

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
predict_laser_image.py. Runs fishsense-core's `LaserDetector` on one frame and
returns the predicted dot in rectified pixels -- the space labelers place laser
labels -- so it can seed the laser Label Studio task as a pre-annotation.

Behaviour is v1's:

* the detector loads once per process, under a lock (double-checked): on a
  cold pod the first batch of activities enters the lazy init together, and
  unguarded each would load its own copy of the checkpoint onto the GPU;
* `LinearRawImage` (linear + Bayer excess, what the 6-channel model was
  trained on), `rectify_output=True` with the dive's intrinsics;
* the colour is read off the dot while the decoded frame is in hand -- at the
  *sensor* point, because the prediction is rectified and the frame is not
  (up to ~48 px apart near the laser region);
* a dot outside the expected-laser region is dropped (`rejected_out_of_region`),
  its confidence kept for audit; no region disables the gate;
* every result carries `LASER_PREDICTOR_VERSION`, the checkpoint and core's
  version.

v2 changes:

* **the weights come from Garage `model-weights`** through core's
  `LaserDetector.from_store` and `weights.GarageWeightStore`, verified against
  core's manifest (PLAN.md §9.12). v1 baked the checkpoint into its image from
  Hugging Face. The settings are read on the first load, not at pod start:
  the processor's stages are registered for every role at import, and only
  the GPU role loads this model;
* the raw frame is an `ObjectRef`, streamed to a temporary file;
* the checkpoint recorded is core's canonical name for it (resolved by
  content), not a path's basename.

torch and the checkpoint are only needed at run time, so `fishsense_core.laser`
and `LinearRawImage` are imported lazily, as in v1.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from functools import cache
from pathlib import Path
from typing import Any

from temporalio import activity

from fishsense_services_contracts.laser import (
    LASER_PREDICTOR_VERSION,
    LaserPredictionResult,
)
from fishsense_services_contracts.laser_region import point_in_laser_region
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.laser_predict.laser_color import (
    classify_laser_color,
    rectified_to_sensor_point,
)
from fishsense_services_processor.laser_predict.workflow import (
    PredictLaserImageInput,
)
from fishsense_services_processor.object_store import ProcessorObjectStore

__all__ = ["predict_laser_image"]

_log = logging.getLogger(__name__)

# Module-level cache: loading is expensive and every activity in the fan-out
# reuses it. The lock is load-bearing, not defensive (see module docstring).
_DETECTOR: Any = None
_DETECTOR_LOCK = threading.Lock()


def _load_detector() -> Any:
    """The pinned detector, from Garage through core's verified fetch."""
    # pylint: disable=import-outside-toplevel
    from fishsense_core.laser import LaserDetector

    from fishsense_services_processor.weights import (
        GarageWeightStore,
        ModelWeightsSettings,
    )

    settings = ModelWeightsSettings()
    return LaserDetector.from_store(
        GarageWeightStore.from_settings(settings), cache_dir=settings.cache_dir
    )


def _get_detector() -> Any:
    """The process-wide detector, loaded on first use (double-checked lock)."""
    global _DETECTOR  # pylint: disable=global-statement
    if _DETECTOR is not None:
        return _DETECTOR
    with _DETECTOR_LOCK:
        if _DETECTOR is None:
            _log.info("loading LaserDetector from the model-weights store")
            _DETECTOR = _load_detector()
    return _DETECTOR


@cache
def _object_store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


def _predict_from_raw(
    raw_path: Path,
    camera_matrix: list[list[float]],
    distortion_coefficients: list[float],
    wavelength: str | None,
) -> Any:
    """Off-loop CPU/GPU work: decode to a `LinearRawImage`, run the detector
    with rectified output, and read the colour at the dot's sensor point.

    Returns `(prediction, width, height, color, color_margin, checkpoint)`.
    `cv2.undistort` preserves the image size, so the rectified dims equal the
    decoded raw dims.
    """
    # pylint: disable=import-outside-toplevel
    import numpy as np
    from fishsense_core.image.linear_raw_image import LinearRawImage

    image = LinearRawImage(raw_path)
    height, width = image.data.shape[:2]
    detector = _get_detector()
    prediction = detector.predict(
        image,
        wavelength=wavelength,
        rectify_output=True,
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion=np.array(distortion_coefficients, dtype=float),
    )

    color = margin = None
    if prediction.x is not None and prediction.y is not None:
        sensor_x, sensor_y = rectified_to_sensor_point(
            prediction.x, prediction.y, camera_matrix, distortion_coefficients
        )
        color, margin = classify_laser_color(image.data, sensor_x, sensor_y)

    checkpoint = getattr(detector, "checkpoint_name", None)
    return prediction, int(width), int(height), color, margin, checkpoint


def _core_version() -> str | None:
    """Installed fishsense-core version: provenance only."""
    try:
        # pylint: disable=import-outside-toplevel
        from importlib.metadata import version

        return version("fishsense-core")
    except Exception:  # pylint: disable=broad-except
        return None


@activity.defn(name="predict_laser_image")
async def predict_laser_image(payload: PredictLaserImageInput) -> LaserPredictionResult:
    """Stream one staged raw frame, run the detector, and return its dot."""
    if not isinstance(payload, PredictLaserImageInput):
        payload = PredictLaserImageInput.model_validate(payload)
    activity.logger.info(
        "predicting laser capture_id=%s raw=%s", payload.capture_id, payload.raw.uri
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = await _object_store().download_raw(payload.raw, Path(tmpdir))
        prediction, width, height, color, margin, checkpoint = await asyncio.to_thread(
            _predict_from_raw,
            raw_path,
            payload.camera_matrix,
            payload.distortion_coefficients,
            payload.wavelength,
        )

    # Gate on the expected-laser region (v1): the detector always answers, and
    # a dot on a reflection or a fin moves the labeler's eye to the wrong place.
    x, y = prediction.x, prediction.y
    rejected = False
    if x is not None and y is not None and payload.laser_region:
        if not point_in_laser_region(x, y, payload.laser_region):
            rejected = True
            x = y = None

    activity.logger.info(
        "predicted laser capture_id=%s x=%s y=%s confidence=%.3f dims=%dx%d "
        "color=%s margin=%s%s",
        payload.capture_id,
        x,
        y,
        prediction.confidence,
        width,
        height,
        color,
        None if margin is None else round(margin, 1),
        " REJECTED_OUT_OF_REGION" if rejected else "",
    )
    return LaserPredictionResult(
        capture_id=payload.capture_id,
        x=x,
        y=y,
        confidence=prediction.confidence,
        width=width,
        height=height,
        color=color,
        color_margin=margin,
        rejected_out_of_region=rejected,
        predictor_version=LASER_PREDICTOR_VERSION,
        checkpoint=checkpoint,
        core_version=_core_version(),
    )
