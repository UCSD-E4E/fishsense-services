"""The automatic-results GPU activity: one raw frame in, its automatic dot and
head/tail out.

New in v2. cscw-fishsense2027@96a8da07 e2e_measurement/run_e2e.py ran the
chain in two passes because its 6 GB card could not hold both models; here
one activity runs both on the same raw frame, each through the stage that
owns it in production:

* the dot: the laser stage's kernel (`laser_predict.activities.
  _predict_from_raw`: `LinearRawImage`, rectified output, the verified
  detector) and its expected-laser region gate;
* the rectified JPEG: the head/tail stage-5.1 rendering
  (`headtail_preprocess.activities.rectify_and_encode_jpeg`), written where
  the orchestrator says when it is not in Garage yet (`write_jpeg`), so the
  species and calibration steps read the very frame SAM saw;
* the mask and head/tail: `automatic_results.frames` (the head/tail kernel
  behind SAM's own score gate), with SAM 3.1's verified checkpoint
  (`headtail_predict.weights`).

**SAM 3.1 only.** The head/tail stage falls back to Mask R-CNN without a GPU;
its lengths were never validated, so here a GPU-less worker refuses,
non-retryably (`NoGpuForSam3`), and the orchestrator does not dispatch to the
CPU fallback at all. A raw frame missing from scratch is an abstention
(`raw_unavailable`), so the dive is not re-selected forever behind it.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import ClientError
from temporalio import activity

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AutomaticFrameResult,
    PredictAutomaticFrameInput,
)
from fishsense_services_contracts.laser_region import point_in_laser_region
from fishsense_services_processor.automatic_results.frames import (
    LaserDot,
    automatic_frame_result,
)
from fishsense_services_processor.headtail_predict.predict import _no_gpu

__all__ = ["AutomaticFramesActivities", "LaserDot"]

#: S3 codes for "not there": the frame was never staged, or was evicted.
_MISSING = {"404", "NoSuchKey", "NotFound"}


class AutomaticFramesActivities:  # pylint: disable=too-few-public-methods
    """The per-frame activity, given its seams: the object store, SAM 3.1's
    verified checkpoint, whether there is a GPU, the dot, the rendering and
    the scored segmenter (production's are wired in `stage`)."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        store_factory: Callable[[], Any],
        sam3_checkpoint: Callable[[], Awaitable[tuple[Path, str]]],
        cuda_available: Callable[[], bool],
        predict_dot: Callable[[Path, list, list], Optional[LaserDot]],
        render_jpeg: Callable[[bytes, list, list], bytes],
        segmenter: Callable[[str], Any],
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._sam3_checkpoint = sam3_checkpoint
        self._cuda_available = cuda_available
        self._predict_dot = predict_dot
        self._render_jpeg = render_jpeg
        self._segmenter = segmenter

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="predict_automatic_frame")
    async def predict_automatic_frame(
        self, payload: PredictAutomaticFrameInput
    ) -> AutomaticFrameResult:
        """Dot, rendering, mask, head/tail for one staged raw frame."""
        if not self._cuda_available():
            raise _no_gpu()
        frame = payload.frame
        store = self._object_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                raw_path = await store.download_raw(frame.raw, Path(tmpdir))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in _MISSING:
                    raise
                activity.logger.warning(
                    "capture=%s: raw %s not in scratch", frame.capture_id, frame.raw.uri
                )
                return AutomaticFrameResult(
                    capture_id=frame.capture_id,
                    status="raw_unavailable",
                    predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
                    slate_probability=frame.slate_probability,
                )
            dot = await asyncio.to_thread(
                self._predict_dot,
                raw_path,
                payload.camera_matrix,
                payload.distortion_coefficients,
            )
            raw_bytes = await asyncio.to_thread(raw_path.read_bytes)
        if (
            dot is not None
            and payload.laser_region
            and not point_in_laser_region(dot.x, dot.y, payload.laser_region)
        ):
            dot = None  # the laser stage's region gate
        jpeg = await asyncio.to_thread(
            self._render_jpeg,
            raw_bytes,
            payload.camera_matrix,
            payload.distortion_coefficients,
        )
        if frame.write_jpeg:
            await store.upload_processed_jpeg(frame.jpeg, jpeg)

        path, model_id = await self._sam3_checkpoint()
        segmenter = await asyncio.to_thread(self._segmenter, str(path))
        result = await asyncio.to_thread(
            lambda: automatic_frame_result(
                capture_id=frame.capture_id,
                dot=dot,
                jpeg_bytes=jpeg,
                segmenter=segmenter,
                checkpoint=model_id,
                core_version=version("fishsense-core"),
                is_slate=frame.is_slate,
                slate_probability=frame.slate_probability,
            )
        )
        activity.logger.info(
            "automatic frame capture=%s status=%s sam_score=%s",
            frame.capture_id,
            result.status,
            result.sam_score,
        )
        return result
