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

**Bounded, and heartbeating** (2026-10-15 audit). Registration is quadratic
in frames -- 100 slate frames are 4,950 pairs -- and a calibration session
can hold hundreds, so the fit takes at most `MAX_CALIBRATION_FRAMES`, spread
evenly along the dot's travel (`spread`) so the range the fit needs is kept;
the paper's sessions had 17-33. And while the fit runs in its thread the
activity keeps heartbeating, where it used to go silent for minutes and be
killed by its heartbeat timeout, three times, sticking the dive.
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

__all__ = [
    "AutomaticCalibrationActivities",
    "HEARTBEAT_EVERY_S",
    "MAX_CALIBRATION_FRAMES",
    "dive_line",
    "spread",
]

#: The most slate frames a fit registers (pairs grow as n^2: 40 are 780).
MAX_CALIBRATION_FRAMES = 40
#: How often the activity heartbeats while the fit runs in its thread.
HEARTBEAT_EVERY_S = 30.0


def spread(dots, cap: int) -> list[int]:
    """At most `cap` of `dots`' indices, evenly spaced along the dots' own
    principal direction (the laser line, along which range varies), the two
    extremes included; all of them when there are no more than `cap`."""
    xy = np.asarray(dots, float).reshape(-1, 2)
    n = len(xy)
    if n <= cap:
        return list(range(n))
    centred = xy - xy.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    order = np.argsort(centred @ vt[0])
    picks = np.round(np.linspace(0, n - 1, cap)).astype(int)
    return sorted(int(order[i]) for i in picks)


async def _while_heartbeating(fn, *args):
    """`fn(*args)` in a thread, heartbeating until it returns."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
        if done:
            return task.result()
        activity.heartbeat()


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
        chosen = [
            payload.frames[i]
            for i in spread(
                [[f.x, f.y] for f in payload.frames], MAX_CALIBRATION_FRAMES
            )
        ]
        features, kept = [], []
        for frame in chosen:
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
        fit = await _while_heartbeating(_fit, payload, features, kept)
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
