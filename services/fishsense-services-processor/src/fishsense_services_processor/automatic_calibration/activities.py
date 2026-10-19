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

**Dots on the dive line only** (version 2, 2026-10-19). A slate frame's dot
farther than `OFF_LINE_PX` from the dive line is not fitted. The fit reads
each dot's place along the line as its range; a dot off the line is not the
laser where the beam meets the slate (a reflection, or the detector on the
slate's pattern), and its range is wrong. Session 77 had 33 of its 60 detector
dots 35-41 px off its line, in one run of frames, and fitted with them its
vanishing point moved from the paper's -2069 px to -3732 px, standard error
1510 px; without them, -2069 px, 0.8 px. cscw never met this: its fits used
human dots on human-labelled slate frames. The line is the dive's (above), or,
with none, the RANSAC line through the frames' own dots (`fit_dive_line`'s
4 px inliers: the most dots that agree); with neither, every dot is kept.
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
    "OFF_LINE_PX",
    "mission_line",
    "on_line",
    "spread",
]

#: The most slate frames a fit registers (pairs grow as n^2: 40 are 780).
MAX_CALIBRATION_FRAMES = 40
#: How often the activity heartbeats while the fit runs in its thread.
HEARTBEAT_EVERY_S = 30.0
#: The farthest a calibration dot may sit from the dive line, px. Detector
#: dots land within 1-2 px of human ones; session 77's off-line block sat at
#: 35-41 px (2026-10-19).
OFF_LINE_PX = 10.0


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


def mission_line(dots) -> Optional[tuple[float, float, float]]:
    """``(a, b, c)`` of the dive's laser line, or None when it can't be told."""
    # pylint: disable-next=import-outside-toplevel
    from fishsense_core.line_fit import fit_dive_line

    xy = np.asarray(dots, float).reshape(-1, 2)
    fit = fit_dive_line(xy)
    if fit is None or not fit.is_confident:
        return None
    return (fit.a, fit.b, fit.c)


def on_line(dots, line, max_px: float = OFF_LINE_PX) -> list[int]:
    """Indices of `dots` within `max_px` of ``a x + b y + c = 0``."""
    xy = np.asarray(dots, float).reshape(-1, 2)
    a, b, c = line
    dist = np.abs(xy @ np.array([a, b]) + c) / np.hypot(a, b)
    return [int(i) for i in np.flatnonzero(dist <= max_px)]


def _features(jpeg: bytes, dot) -> sc.FrameFeatures:
    import cv2  # pylint: disable=import-outside-toplevel

    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("the JPEG does not decode")
    return sc.extract_features(image, dot)


def _line(payload: FitAutomaticCalibrationInput):
    """The dive's line, else the frames' own, else None."""
    line = mission_line(payload.line_dots) if payload.line_dots else None
    if line is None and len(payload.frames) >= 2:
        line = mission_line([[f.x, f.y] for f in payload.frames])
    return line


def _fit(features, kept, line, camera_matrix):
    dots = np.array([[f.x, f.y] for f in kept], float)
    pairs = sc.pair_scales(features, dots)
    return sc.fit_label_free(
        dots=dots, pairs=pairs, camera_matrix=camera_matrix, line=line
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
            "mission_id": payload.mission_id,
            "algorithm_version": AUTOMATIC_CALIBRATION_VERSION,
            "candidate_count": len(payload.frames),
            "core_version": version("fishsense-core"),
        }
        if not payload.frames:
            return AutomaticCalibrationResult(
                outcome="refused", refusal_reason="no_candidates", **base
            )
        line = await asyncio.to_thread(_line, payload)
        frames = payload.frames
        if line is not None:
            frames = [frames[i] for i in on_line([[f.x, f.y] for f in frames], line)]
            if len(frames) < len(payload.frames):
                activity.logger.info(
                    "dive=%s: %d of %d slate dots off the dive line, not fitted",
                    payload.mission_id,
                    len(payload.frames) - len(frames),
                    len(payload.frames),
                )
        if len(frames) < sc.MIN_FRAMES:
            return AutomaticCalibrationResult(
                outcome="refused", refusal_reason="too_few_frames", **base
            )
        store = self._object_store()
        chosen = [
            frames[i]
            for i in spread([[f.x, f.y] for f in frames], MAX_CALIBRATION_FRAMES)
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
        fit = await _while_heartbeating(
            _fit, features, kept, line, payload.camera_matrix
        )
        activity.logger.info(
            "dive=%s label-free %s (%s): frames=%d pairs=%d ratio=%s",
            payload.mission_id, fit.outcome, fit.refusal_reason, fit.frames_used,
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
