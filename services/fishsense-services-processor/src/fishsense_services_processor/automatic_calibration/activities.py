"""The label-free calibration activity: a dive's slate frames in, a beam out.

New in v2. cscw-fishsense2027@96a8da07 calibration_analysis/
e1j_size_constancy/register.py ran it in two passes (SIFT from the NAS, then
the fit); here one activity per dive reads each slate frame's rectified JPEG
(the head/tail rendering the GPU stage wrote), takes SIFT around its automatic
dot, registers every pair and fits (`automatic_results.size_constancy`).

The dive line is fishsense-core's `fit_dive_line` through every automatic dot
of the dive -- cscw used production's dive line, fitted by the same function
from labels -- unless there are too few dots or the fit is not confident;
then the calibration frames' own dots (cscw's fallback).

On the per-image role: it decodes images (a JPEG at a time, keeping only the
features about the dot), which the light role's memory budget excludes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from importlib.metadata import version
from typing import Any, Optional

import numpy as np
from temporalio import activity

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_CALIBRATION_VERSION,
    AutomaticCalibrationResult,
    FitAutomaticCalibrationInput,
)
from fishsense_services_processor.automatic_results import size_constancy as sc

__all__ = ["AutomaticCalibrationActivities", "dive_line"]


def dive_line(dots) -> Optional[tuple[float, float, float]]:
    """``(a, b, c)`` of the dive's laser line, or None when it can't be told."""
    # pylint: disable-next=import-outside-toplevel
    from fishsense_core.line_fit import fit_dive_line

    xy = np.asarray(dots, float).reshape(-1, 2)
    fit = fit_dive_line(xy)
    if fit is None or not fit.is_confident:
        return None
    return (fit.a, fit.b, fit.c)


def _features(jpeg: bytes, dot) -> sc.FrameFeatures:
    import cv2  # pylint: disable=import-outside-toplevel

    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("the JPEG does not decode")
    return sc.extract_features(image, dot)


def _fit(payload: FitAutomaticCalibrationInput, features, kept):
    dots = np.array([[f.x, f.y] for f in kept], float)
    pairs = sc.pair_scales(features, dots)
    line = dive_line(payload.line_dots) if payload.line_dots else None
    return sc.fit_label_free(
        dots=dots, pairs=pairs, camera_matrix=payload.camera_matrix, line=line
    )


class AutomaticCalibrationActivities:  # pylint: disable=too-few-public-methods
    def __init__(self, *, store_factory: Callable[[], Any]) -> None:
        self._store_factory = store_factory
        self._store: Any = None

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="fit_automatic_calibration")
    async def fit_automatic_calibration(
        self, payload: FitAutomaticCalibrationInput
    ) -> AutomaticCalibrationResult:
        """The dive's label-free calibration from its slate frames, or why not."""
        base = {
            "dive_id": payload.dive_id,
            "algorithm_version": AUTOMATIC_CALIBRATION_VERSION,
            "candidate_count": len(payload.frames),
            "core_version": version("fishsense-core"),
        }
        if not payload.frames:
            return AutomaticCalibrationResult(
                outcome="refused", refusal_reason="no_candidates", **base
            )
        store = self._object_store()
        features, kept = [], []
        for frame in payload.frames:
            try:
                jpeg = await store.download_processed_jpeg(frame.jpeg)
                features.append(
                    await asyncio.to_thread(_features, jpeg, (frame.x, frame.y))
                )
                kept.append(frame)
            except Exception as exc:  # pylint: disable=broad-except
                # One unreadable frame costs one frame, not the dive.
                activity.logger.warning(
                    "capture=%s: no features (%s)", frame.capture_id, exc
                )
            activity.heartbeat()
        fit = await asyncio.to_thread(_fit, payload, features, kept)
        activity.logger.info(
            "dive=%s label-free %s (%s): frames=%d pairs=%d ratio=%s",
            payload.dive_id, fit.outcome, fit.refusal_reason, fit.frames_used,
            fit.pairs, fit.size_ratio,
        )  # fmt: skip
        return AutomaticCalibrationResult(
            outcome=fit.outcome,
            refusal_reason=fit.refusal_reason,
            laser_position=fit.laser_position,
            laser_axis=fit.laser_axis,
            vanishing_px=fit.vanishing_px,
            line_direction=fit.line_direction,
            line_offset_px=fit.line_offset_px,
            o_mag_m=fit.o_mag_m,
            frames_used=fit.frames_used,
            pair_count=fit.pairs,
            size_ratio=fit.size_ratio,
            se_px=fit.se_px,
            pair_residual_sd=fit.pair_residual_sd,
            capture_ids=[kept[i].capture_id for i in fit.used],
            **base,
        )
