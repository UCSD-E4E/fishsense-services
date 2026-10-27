"""The label-free calibration activity: a dive's slate frames in, a beam out.

New in v2. cscw-fishsense2027@96a8da07 calibration_analysis/
e1j_size_constancy/register.py ran it in two passes (SIFT from the NAS, then
the fit); here one activity per dive reads each slate frame's rectified JPEG
(the head/tail rendering the GPU stage wrote), takes SIFT around its automatic
dot, registers every pair and fits (fishsense-core's `size_constancy`, which
absorbed this repo's port, core #97).

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
1510 px; without them, on cscw's features and the labelled line, -2069 px,
0.8 px (77 itself is refused below). cscw never met this: its fits used human
dots on human-labelled slate frames. The dots are gated on the dive's line
(above), or, with none, the RANSAC line through the frames' own dots
(`fit_dive_line`'s 4 px inliers: the most dots that agree) -- which gates
only: the fit is then given no line and takes cscw's fallback, the direction
through the dots it uses. With neither, every dot is kept.

**Unless the line is ambiguous.** When the dots off the line hold a line of
their own of at least `MIN_FRAMES` dots and a quarter of those on it
(`second_line`), the laser cannot be told from its reflection without labels
and the fit refuses (``ambiguous_line``). Session 77: the off-line dots formed
a line of 30 to the chosen line's 48, and the chosen one was the wrong line,
1.35 deg from the one fitted from labels (the fit was 0.42 deg off the
known-size calibration). On every other calibrated mission the off-line dots
formed a line of at most 6, a fifth of the line's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from importlib.metadata import version
from typing import Any, Optional

import numpy as np
from fishsense_core import size_constancy as sc
from temporalio import activity

from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_CALIBRATION_VERSION,
    AutomaticCalibrationResult,
    FitAutomaticCalibrationInput,
)
from fishsense_services_contracts.calibration_v3 import (
    CalibrationDraw,
    CalibrationV3Report,
    ReplayCalibrationInput,
)
from fishsense_services_processor.automatic_calibration import pose, v3

__all__ = [
    "AutomaticCalibrationActivities",
    "HEARTBEAT_EVERY_S",
    "MAX_CALIBRATION_FRAMES",
    "OFF_LINE_PX",
    "SECOND_LINE_SHARE",
    "mission_line",
    "on_line",
    "replay_v3",
    "second_line",
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
#: The share of the line's dots a second line among the rest must reach (as
#: well as `size_constancy.MIN_FRAMES`) to make the line ambiguous.
SECOND_LINE_SHARE = 0.25


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


def second_line(dots) -> int:
    """How many of `dots` lie on the RANSAC line through them (none: 0)."""
    # pylint: disable-next=import-outside-toplevel
    from fishsense_core.line_fit import fit_dive_line

    xy = np.asarray(dots, float).reshape(-1, 2)
    if len(xy) < 2:
        return 0
    fit = fit_dive_line(xy)
    return 0 if fit is None else int(fit.inlier_count)


def _features(jpeg: bytes, dot) -> sc.FrameFeatures:
    import cv2  # pylint: disable=import-outside-toplevel

    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("the JPEG does not decode")
    return sc.extract_features(image, dot)


def _lines(payload: FitAutomaticCalibrationInput):
    """``(gate, fit)``: the line the dots are gated on -- the dive's, else the
    RANSAC line through the frames' own dots -- and the one the fit is given:
    the dive's, else None, so the fit takes its direction through the dots it
    uses (cscw's fallback; review of #136)."""
    fit = mission_line(payload.line_dots) if payload.line_dots else None
    gate = fit
    if gate is None and len(payload.frames) >= 2:
        gate = mission_line([[f.x, f.y] for f in payload.frames])
    return gate, fit


def _fit(features, kept, line, camera_matrix):
    dots = np.array([[f.x, f.y] for f in kept], float)
    pairs = sc.pair_scales(features, dots)
    return sc.fit_label_free(
        dots=dots, pairs=pairs, camera_matrix=camera_matrix, line=line
    )


def _others(line_dots, frames) -> list:
    """The dive's dots that are not its slate frames' (each slate dot is in
    `line_dots` once: the same row)."""
    slate: dict = {}
    for f in frames:
        slate[(f.x, f.y)] = slate.get((f.x, f.y), 0) + 1
    out = []
    for x, y in line_dots:
        if slate.get((x, y), 0):
            slate[(x, y)] -= 1
        else:
            out.append([x, y])
    return out


def replay_v3(
    payload: FitAutomaticCalibrationInput,
    features: list,
    kept: list,
    *,
    draw_sizes=(),
    draws_per_size: int = 0,
    pairs_of=None,
    sizes=None,
    points=None,
) -> CalibrationV3Report:
    """v3 on the mission's slate frames whose features were extracted
    (`kept`, parallel to `features`), and on random draws of them. Blocking.
    `pairs_of(features, dots)` is `size_constancy.pair_scales` (a seam).
    With `sizes` (parallel to `kept`: each frame's SAM mask size, None with
    no mask) the fit is on those, not on pairs (strategy 2). With `points`
    (parallel to `kept`: each frame's dot on its automatically posed slate,
    camera-space m; None: not posed) the beam is fitted through them
    (calibration v4, `pose.fit_pose_points`): v3's screen, consensus and
    check are kept, two posed slates are an answer, and the fit carries its
    error bar (`scale_sd`; 2026-10-27)."""
    pairs_of = pairs_of or sc.pair_scales
    dots = np.array([[f.x, f.y] for f in kept], float).reshape(-1, 2)
    dive_line = mission_line(payload.line_dots) if payload.line_dots else None
    others = _others(payload.line_dots, payload.frames)

    def fit(indices):
        idx = list(indices)
        if points is not None:
            return pose.fit_pose_points([points[i] for i in idx],
                                        min_frames=pose.MIN_POSE_FRAMES,
                                        camera_matrix=payload.camera_matrix)  # fmt: skip
        if sizes is not None:
            # No gate in the replay: its draws are what set the threshold.
            return v3.fit_known_sizes(dots[idx], [sizes[i] for i in idx],
                                      payload.camera_matrix, scale_sd_max=np.inf)  # fmt: skip
        return v3.LabelFreeFit.of(sc.fit_label_free(
            dots=dots[idx], pairs=pairs_of([features[i] for i in idx], dots[idx]),
            camera_matrix=payload.camera_matrix, min_frames=v3.MIN_FRAMES,
        ))  # fmt: skip

    def run(indices):
        idx = list(indices)

        def sub_fit(local):
            return fit([idx[i] for i in local])

        return v3.calibrate_v3(slate_xy=dots[idx], dive_line=dive_line, other_xy=others,
                               camera_matrix=payload.camera_matrix, fit=sub_fit,
                               min_frames=(v3.MIN_FRAMES if points is None
                                           else pose.MIN_POSE_FRAMES))  # fmt: skip

    main = run(range(len(kept)))
    f = main.fit
    accepted = main.outcome == "accepted"
    report = CalibrationV3Report(
        mission_id=payload.mission_id, outcome=main.outcome,
        size_source="pose" if points is not None else "sift" if sizes is None else "sam",
        refusal_reason=main.refusal_reason, candidate_count=len(payload.frames),
        laser_position=f.laser_position if accepted else None,
        laser_axis=f.laser_axis if accepted else None,
        vanishing_px=None if f is None else f.vanishing_px,
        line_direction=None if f is None else f.line_direction,
        line_offset_px=None if f is None else f.line_offset_px,
        frames_used=0 if f is None else f.frames_used,
        pair_count=0 if f is None else f.pairs,
        size_ratio=None if f is None else f.size_ratio,
        se_px=None if f is None else f.se_px,
        scale_sd=None if f is None else f.scale_sd,
        gross_dropped=main.gross_dropped, consensus_dropped=main.consensus_dropped,
        rival=main.rival, check_dots=main.check.dots,
        check_median_px=main.check.median_px,
        check_within_share=main.check.within_share,
    )  # fmt: skip
    draws = []
    for size in draw_sizes:
        if size > len(kept):
            continue
        for seed in range(draws_per_size):
            rng = np.random.default_rng([size, seed])
            d = run(sorted(rng.choice(len(kept), size, replace=False).tolist()))
            ok = d.outcome == "accepted"
            draws.append(CalibrationDraw(
                size=size, seed=seed, outcome=d.outcome, refusal_reason=d.refusal_reason,
                laser_position=d.fit.laser_position if ok else None,
                laser_axis=d.fit.laser_axis if ok else None,
                vanishing_px=None if d.fit is None else d.fit.vanishing_px,
                frames_used=0 if d.fit is None else d.fit.frames_used,
                scale_sd=None if d.fit is None else d.fit.scale_sd,
                check_median_px=d.check.median_px,
                check_within_share=d.check.within_share,
            ))  # fmt: skip
    return report.model_copy(update={"draws": draws})


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
        line, fit_line = await asyncio.to_thread(_lines, payload)
        frames = payload.frames
        off: list = []
        if line is not None:
            kept = set(on_line([[f.x, f.y] for f in frames], line))
            off = [f for i, f in enumerate(frames) if i not in kept]
            frames = [f for i, f in enumerate(frames) if i in kept]
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
        rival = await asyncio.to_thread(second_line, [[f.x, f.y] for f in off])
        if rival >= max(sc.MIN_FRAMES, SECOND_LINE_SHARE * len(frames)):
            activity.logger.info(
                "dive=%s: off-line dots form a line of %d to the line's %d",
                payload.mission_id, rival, len(frames),
            )  # fmt: skip
            return AutomaticCalibrationResult(
                outcome="refused", refusal_reason="ambiguous_line", **base
            )
        store = self._object_store()
        chosen = [
            frames[i]
            for i in spread([[f.x, f.y] for f in frames], MAX_CALIBRATION_FRAMES)
        ]
        features, kept = [], []
        for frame in chosen:
            try:
                jpeg = await store.download_image(frame.jpeg, frame.jpeg_url)
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
            _fit, features, kept, fit_line, payload.camera_matrix
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

    @activity.defn(name="replay_automatic_calibration_v3")
    async def replay_automatic_calibration_v3(
        self, request: ReplayCalibrationInput
    ) -> CalibrationV3Report:
        """What label-free calibration v3 would make of the mission, and of
        random draws of its slate frames: read-only, nothing is stored.
        Features from at most `MAX_CALIBRATION_FRAMES`, spread along the dots'
        travel, as v2's."""
        payload = request.payload
        if request.size_source == "pose":
            # Calibration v4: points on slates posed on the GPU already.
            points = [
                request.pose_points.get(str(f.capture_id)) for f in payload.frames
            ]
            report = await _while_heartbeating(
                lambda: replay_v3(payload, [], payload.frames, points=points,
                                  draw_sizes=request.draw_sizes,
                                  draws_per_size=request.draws_per_size)  # fmt: skip
            )
            activity.logger.info("dive=%s v4 replay (pose) %s (%s): frames=%d",
                                 payload.mission_id, report.outcome,
                                 report.refusal_reason, report.frames_used)  # fmt: skip
            return report
        if request.size_source == "sam":
            # Strategy 2: sizes from SAM masks, measured on the GPU already;
            # no image is read here.
            sizes = [request.sam_sizes.get(str(f.capture_id)) for f in payload.frames]
            report = await _while_heartbeating(
                lambda: replay_v3(payload, [], payload.frames, sizes=sizes,
                                  draw_sizes=request.draw_sizes,
                                  draws_per_size=request.draws_per_size)  # fmt: skip
            )
            activity.logger.info("dive=%s v3 replay (sam sizes) %s (%s): frames=%d",
                                 payload.mission_id, report.outcome,
                                 report.refusal_reason, report.frames_used)  # fmt: skip
            return report
        store = self._object_store()
        chosen = [payload.frames[i] for i in
                  spread([[f.x, f.y] for f in payload.frames], MAX_CALIBRATION_FRAMES)]  # fmt: skip
        features, kept = [], []
        for frame in chosen:
            try:
                jpeg = await store.download_image(frame.jpeg, frame.jpeg_url)
                features.append(
                    await asyncio.to_thread(_features, jpeg, (frame.x, frame.y))
                )
                kept.append(frame)
            except Exception as exc:  # pylint: disable=broad-except
                activity.logger.warning(
                    "capture=%s: no features (%s)", frame.capture_id, exc
                )
            activity.heartbeat()
        report = await _while_heartbeating(
            lambda: replay_v3(payload, features, kept, draw_sizes=request.draw_sizes,
                              draws_per_size=request.draws_per_size)  # fmt: skip
        )
        activity.logger.info(
            "dive=%s v3 replay %s (%s): frames=%d check=%s/%s draws=%d",
            payload.mission_id, report.outcome, report.refusal_reason,
            report.frames_used, report.check_median_px, report.check_within_share,
            len(report.draws),
        )  # fmt: skip
        return report
