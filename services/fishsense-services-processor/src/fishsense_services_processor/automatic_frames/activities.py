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
  the orchestrator says, so the species and calibration steps read the very
  frame SAM saw. It is render `AUTOMATIC_JPEG_RENDER_VERSION` under the
  payload's camera calibration, keyed by both
  (`object_store.automatic_jpeg_folder`): a frame asking for another render,
  or whose key is not that render's, is refused and retried
  (`RenderVersionMismatch`), so a worker at another version -- a lab worker
  deployed by hand -- never writes its render where another is expected; the
  result names the render and camera it wrote (2026-10-19). A contract-7
  frame (no render named; in flight across the deploy) runs as it did --
  written only if `write_jpeg` -- and names none;
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
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import ClientError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AUTOMATIC_JPEG_RENDER_VERSION,
    AutomaticFrameResult,
    PredictAutomaticFrameInput,
)
from fishsense_services_contracts.laser_region import point_in_laser_region
from fishsense_services_contracts.object_store import automatic_jpeg_folder
from fishsense_services_processor.slate_detect.frame import DECODE_ERRORS
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
        #: Called after a CUDA failure has been reported: the process's CUDA
        #: context is unusable from then on (2026-10-19, one launch failure
        #: failed every frame after it on the pod), so the worker exits and
        #: Kubernetes restarts it clean.
        self._on_cuda_failure: Callable[[], None] = _exit_soon

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
        # Contract 8 names the render; contract 7 (in flight across the
        # deploy) does not, and runs as it did, naming none.
        versioned = frame.jpeg_render_version is not None
        if versioned:
            _check_render(payload)
        write = versioned or frame.write_jpeg
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
            try:
                dot = await asyncio.to_thread(
                    self._predict_dot,
                    raw_path,
                    payload.camera_matrix,
                    payload.distortion_coefficients,
                )
                raw_bytes = await asyncio.to_thread(raw_path.read_bytes)
                jpeg = await asyncio.to_thread(
                    self._render_jpeg,
                    raw_bytes,
                    payload.camera_matrix,
                    payload.distortion_coefficients,
                )
            except DECODE_ERRORS as exc:
                # Recorded, not raised: raised, it retries to the child's
                # timeout and the dive is re-selected every hour, stalling the
                # backlog behind one corrupt file (the slate stage's rule).
                activity.logger.warning(
                    "capture=%s: raw would not decode: %s", frame.capture_id, exc
                )
                return AutomaticFrameResult(
                    capture_id=frame.capture_id,
                    status="decode_failed",
                    predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
                    slate_probability=frame.slate_probability,
                )
        if (
            dot is not None
            and payload.laser_region
            and not point_in_laser_region(dot.x, dot.y, payload.laser_region)
        ):
            dot = None  # the laser stage's region gate
        if write:
            await store.upload_processed_jpeg(frame.jpeg, jpeg)

        path, model_id = await self._sam3_checkpoint()
        segmenter = await asyncio.to_thread(self._segmenter, str(path))
        _reset_gpu_peak()
        try:
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
        except RuntimeError as exc:
            if "CUDA error" in str(exc):
                activity.logger.error(
                    "capture=%s: CUDA failed (%s); restarting the worker for a "
                    "clean context",
                    frame.capture_id,
                    str(exc).splitlines()[0],
                )
                self._on_cuda_failure()
            raise
        peak = _gpu_peak_bytes()
        activity.logger.info(
            "automatic frame capture=%s status=%s sam_score=%s peak_gpu_gib=%s",
            frame.capture_id,
            result.status,
            result.sam_score,
            "-" if peak is None else f"{peak / 2**30:.2f}",
        )
        if versioned:
            result = result.model_copy(
                update={
                    "jpeg_render_version": AUTOMATIC_JPEG_RENDER_VERSION,
                    "jpeg_camera_calibration_id": payload.camera_calibration_id,
                }
            )
        return result


def _check_render(payload: PredictAutomaticFrameInput) -> None:
    """The frame asks for this worker's render, keyed under its camera, or the
    activity fails -- retryably: another worker may be at the asked version."""
    frame = payload.frame
    camera = payload.camera_calibration_id
    expected = (
        None
        if camera is None
        else automatic_jpeg_folder(AUTOMATIC_JPEG_RENDER_VERSION, camera)
    )
    folder = frame.jpeg.key.split("/")[-2] if "/" in frame.jpeg.key else None
    if frame.jpeg_render_version != AUTOMATIC_JPEG_RENDER_VERSION or (
        expected is None or folder != expected
    ):
        raise ApplicationError(
            f"capture {frame.capture_id}: asked for render "
            f"{frame.jpeg_render_version} at {frame.jpeg.key} (camera {camera}); "
            f"this worker renders {AUTOMATIC_JPEG_RENDER_VERSION}, to {expected}",
            type="RenderVersionMismatch",
        )


def _exit_soon(delay_s: float = 5.0) -> None:
    """Exit the process shortly, once the worker has reported the failure.
    A plain exit: a graceful shutdown would wait on work that cannot succeed
    on this CUDA context."""
    asyncio.get_running_loop().call_later(delay_s, os._exit, 70)


def _cuda():
    """torch.cuda if torch is loaded with a card (never imported here: the
    worker imports torch first, and tests run without it)."""
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return None
    return torch.cuda


def _reset_gpu_peak() -> None:
    if (cuda := _cuda()) is not None:
        cuda.reset_peak_memory_stats()


def _gpu_peak_bytes() -> int | None:
    """The process's peak GPU memory since the last reset: one frame's at a
    cap of one, every in-flight frame's together above it (review of #110:
    the GPU cap is tuned from this)."""
    cuda = _cuda()
    return None if cuda is None else int(cuda.max_memory_allocated())
