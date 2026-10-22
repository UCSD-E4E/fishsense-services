"""The shadow calibration's fit step: both dot sources, both variants.

docs/shadow-size-constancy.md. Per mission, for each dot source:

- ``labels``: production's label dots on the mission's own laser line
  (`current_mission_laser_lines`: its direction, with the offset the median of
  the dots' normal coordinate, as register.py:153-154), or with none, the
  direction through the dots (slate_unknown.frame's fallback);
- ``detector``: the automatic track's dots, through the committed consistency
  gate (`gated.detector_gate`), on the detector line it fits;

and for each variant: ``port`` (the script: no gate, no trim, no refusal but
too few frames to fit at all) and ``gated`` (§13's gate, trim and refusals).
Every field the backfill report needs is filled here; the orchestrator only
stores it.

Every mission gets all four rows (review of #261): a source with no dot is
refused ``no_dots``, detector dots with no detector line ``no_detector_line``
-- a mission with nothing stored would be picked again every hour. A frame
whose render SAM could not decode is left out of both variants as
``decode_failed``, not counted as no mask.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Optional

import numpy as np

from fishsense_services_contracts.shadow_calibration import (
    SHADOW_ALGORITHM_VERSION,
    SHADOW_RENDER_LONG_SIDE,
    SHADOW_SAM_PROMPTS,
    SHADOW_SAM_THRESHOLD,
    FitShadowCalibrationInput,
    ShadowCalibrationOut,
    ShadowFitFrame,
    ShadowFitResult,
    ShadowFrameOut,
)
from fishsense_services_processor.shadow_calibration.fit import (
    O_DESIGN_M,
    VARIANT_INDEX,
    port_fit,
)
from fishsense_services_processor.shadow_calibration.gated import (
    COMMITTED,
    Frame,
    detector_gate,
    gated_fit,
    laser_points,
)

__all__ = ["run_shadow_fit"]

#: Each (source, variant)'s bootstrap seed index: the script's for its
#: nearest variant (labels on the mission line: sam3_all; on the detector
#: line: sam3_predline).
_SEED = {
    "labels": VARIANT_INDEX["sam3_all"],
    "detector": VARIANT_INDEX["sam3_predline"],
}


def _code_version() -> str:
    return f"fishsense-services-processor {version('fishsense-services-processor')}"


def _line_from_mission(line, xy) -> tuple[np.ndarray, float]:
    a, b, _ = line
    n = np.array([a, b], float) / np.hypot(a, b)
    u = np.array([n[1], -n[0]])
    u = u if u[1] < 0 else -u
    return u, float(np.median(xy @ np.array([-u[1], u[0]])))


def _line_from_dots(xy) -> tuple[np.ndarray, float]:
    u = np.linalg.svd(xy - xy.mean(0))[2][0]
    u = u if u[1] < 0 else -u
    return u, float(np.median(xy @ np.array([-u[1], u[0]])))


def _frame_out(f: ShadowFitFrame, **extra) -> ShadowFrameOut:
    return ShadowFrameOut(**{**f.model_dump(include=set(ShadowFrameOut.model_fields)
                                           - {"used", "rejected_reason"}), **extra})  # fmt: skip


def _rejected_counts(frames_out) -> dict[str, int]:
    rejected: dict[str, int] = {}
    for f in frames_out:
        if not f.used:
            rejected[f.rejected_reason] = rejected.get(f.rejected_reason, 0) + 1
    return rejected


def _common(payload, source, variant) -> dict:
    return dict(
        dot_source=source, variant=variant, o_mag_m=O_DESIGN_M,
        sam_model=payload.sam_model, sam_prompts=list(SHADOW_SAM_PROMPTS),
        sam_threshold=SHADOW_SAM_THRESHOLD, render_long_side=SHADOW_RENDER_LONG_SIDE,
        algorithm_version=SHADOW_ALGORITHM_VERSION, code_version=_code_version(),
        camera_calibration_id=payload.camera_calibration_id,
        reference_laser_calibration_id=payload.reference_laser_calibration_id,
        reference_borrowed=payload.reference_borrowed,
    )  # fmt: skip


def _refused(payload, source, reason, frames: list[ShadowFitFrame]) -> list:
    """Both variants of a source with nothing to fit, every frame listed."""
    out = []
    for variant in ("port", "gated"):
        frames_out = [_frame_out(f, used=False, rejected_reason=reason) for f in frames]
        out.append(ShadowCalibrationOut(
            **_common(payload, source, variant), outcome="refused",
            refusal_reason=reason, refusals=[reason], frames_used=0,
            frames_rejected=_rejected_counts(frames_out), frames=frames_out,
        ))  # fmt: skip
    return out


def _calibration(payload, source, variant, *, u, off, line_source, frames_out,
                 result, gate=None) -> ShadowCalibrationOut:  # fmt: skip
    fit = result.fit
    ranges = list(result.ranges_m.values())
    out = dict(
        **_common(payload, source, variant), outcome=result.outcome,
        refusal_reason=result.refusal_reason, refusals=list(result.refusals),
        frames_used=len(result.used),
        line_direction=[float(v) for v in u], line_offset_px=float(off),
        line_source=line_source,
        frames_rejected=_rejected_counts(frames_out), lever_m=result.lever_m,
        lever_m_production_offset=result.lever_m_production_offset,
        size_cv_pct=result.size_cv_pct, size_maxdev_pct=result.size_maxdev_pct,
        sam_score_min=result.sam_score_min, sam_score_median=result.sam_score_median,
        no_mask_share=result.no_mask_share,
        range_min_m=min(ranges) if ranges else None,
        range_max_m=max(ranges) if ranges else None,
        gate=gate or {}, frames=frames_out,
    )  # fmt: skip
    if fit is not None:
        out.update(
            laser_position=[float(v) for v in fit.origin],
            laser_axis=[float(v) for v in fit.axis],
            vanishing_px=fit.tv, angle_diff_deg=fit.angle_deg,
            vp_offset_px=fit.err_px, vp_offset_deg_fx=fit.err_deg_fx,
            vp_offset_deg_f2850=fit.err_deg_f2850, fit_residual=fit.residual,
            se_px=fit.se_px, se_deg=fit.se_deg, size_spread=fit.size_spread,
        )  # fmt: skip
    if result.outcome == "accepted" and fit is None:
        raise AssertionError("an accepted fit has a beam")
    return ShadowCalibrationOut(**out)


def _variants(payload, source, frames: list[ShadowFitFrame], *, u, off, line_source,
              pre_rejected: dict, ambiguous: bool = False, gate=None) -> list:  # fmt: skip
    """Both variants of one source, over its frames (some pre-rejected)."""
    K = np.array(payload.camera_matrix, float)
    n = np.array([-u[1], u[0]])
    ref_axis = (
        None if payload.reference_axis is None else np.array(payload.reference_axis)
    )
    ref_pos = (
        None
        if payload.reference_position is None
        else np.array(payload.reference_position)
    )
    seed = (payload.mission_number, _SEED[source])
    out = []
    undecoded = {f.capture_id: "decode_failed" for f in frames
                 if f.segment_status == "decode_failed"}  # fmt: skip
    for variant in ("port", "gated"):
        # The gate is the gated variant's: the port is the script, ungated.
        rejected = {**(pre_rejected if variant == "gated" else {}), **undecoded}
        candidates = [Frame(capture_id=f.capture_id, x=f.dot_x, y=f.dot_y, size=f.size,
                            sam_score=f.sam_score, frame_source=f.frame_source)
                      for f in frames if f.capture_id not in rejected]  # fmt: skip
        if variant == "gated":
            result = gated_fit(candidates, u=u, off=off, K=K, reference_axis=ref_axis,
                               reference_position=ref_pos, seed=seed,
                               size_to_full_px=payload.render_scale,
                               ambiguous_line=ambiguous)  # fmt: skip
        else:
            result = _port(candidates, u=u, off=off, K=K, ref_axis=ref_axis, seed=seed)
        used, trimmed = set(result.used), set(result.trimmed)
        frames_out = []
        for f in frames:
            reason: Optional[str] = rejected.get(f.capture_id)
            if reason is None and f.size is None:
                reason = "no_mask_at_dot"
            elif reason is None and f.capture_id in trimmed:
                reason = "range_inconsistent"
            elif reason is None and f.capture_id not in used:
                reason = "not_fitted"
            t = None if f.dot_x is None else float(np.array([f.dot_x, f.dot_y]) @ u)
            dist = (None if f.dot_x is None
                    else float(abs(np.array([f.dot_x, f.dot_y]) @ n - off)))  # fmt: skip
            frames_out.append(_frame_out(
                f, used=reason is None, rejected_reason=reason, t_px=t,
                line_distance_px=dist, range_m=result.ranges_m.get(f.capture_id),
            ))  # fmt: skip
        out.append(_calibration(payload, source, variant, u=u, off=off,
                                line_source=line_source, frames_out=frames_out,
                                result=result,
                                gate=gate if variant == "gated" else None))  # fmt: skip
    return out


def _port(frames, *, u, off, K, ref_axis, seed):
    """The script's fit: every frame with a size, no gate, no trim, no
    refusal but too few frames to fit at all."""
    from fishsense_services_processor.shadow_calibration.gated import (  # pylint: disable=import-outside-toplevel
        GatedResult,
    )

    sized = [f for f in frames if f.size is not None]
    no_mask = tuple(f.capture_id for f in frames if f.size is None)
    share = len(no_mask) / len(frames) if frames else 1.0
    if len(sized) < 3:
        return GatedResult(outcome="refused", refusal_reason="too_few_frames",
                           refusals=("too_few_frames",), fit=None, used=(), trimmed=(),
                           no_mask=no_mask, no_mask_share=share)  # fmt: skip
    xy = np.array([[f.x, f.y] for f in sized])
    s = np.array([f.size for f in sized])
    fit = port_fit(xy, s, u, off, K, ref_axis, seed=seed)
    z = laser_points(xy, K, fit.origin, fit.axis)[:, 2]
    scores = [f.sam_score for f in sized if f.sam_score is not None]
    return GatedResult(
        outcome="accepted", refusal_reason=None, refusals=(), fit=fit,
        used=tuple(f.capture_id for f in sized), trimmed=(), no_mask=no_mask,
        lever_m=float(z.max() - z.min()), no_mask_share=share,
        sam_score_min=float(min(scores)) if scores else None,
        sam_score_median=float(np.median(scores)) if scores else None,
        ranges_m={f.capture_id: float(zi) for f, zi in zip(sized, z)},
    )  # fmt: skip


def run_shadow_fit(payload: FitShadowCalibrationInput) -> ShadowFitResult:
    calibrations = []
    labels = [f for f in payload.frames.get("labels", []) if f.dot_x is not None]
    if labels:
        xy = np.array([[f.dot_x, f.dot_y] for f in labels])
        if payload.mission_line is not None:
            u, off = _line_from_mission(payload.mission_line, xy)
            source = "mission_line"
        else:
            u, off = _line_from_dots(xy)
            source = "dots"
        calibrations += _variants(payload, "labels", labels, u=u, off=off,
                                  line_source=source, pre_rejected={})  # fmt: skip
    else:
        calibrations += _refused(payload, "labels", "no_dots", [])
    detector = [f for f in payload.frames.get("detector", []) if f.dot_x is not None]
    gate = (
        detector_gate(
            slate_dots=[(f.capture_id, f.dot_x, f.dot_y, f.dot_confidence or 0.0)
                        for f in detector],
            mission_dots=np.array(payload.mission_detector_dots, float),
        )  # fmt: skip
        if detector
        else None
    )
    if not detector:
        calibrations += _refused(payload, "detector", "no_dots", [])
    elif gate is None:
        calibrations += _refused(payload, "detector", "no_detector_line", detector)
    else:
        counts: dict[str, int] = {}
        for r in gate.rejected.values():
            counts[r] = counts.get(r, 0) + 1
        gate_record = {
            "min_confidence": COMMITTED.min_confidence,
            "off_line_px": COMMITTED.off_line_px,
            "second_line_min": COMMITTED.second_line_min,
            "second_line_share": COMMITTED.second_line_share,
            "residual_max": COMMITTED.residual_max,
            "line_points": gate.line_points, "line_of": gate.line_of,
            "second_line": gate.second_line, "ambiguous_line": gate.ambiguous_line,
            "rejected": counts,
        }  # fmt: skip
        calibrations += _variants(payload, "detector", detector, u=gate.u, off=gate.offset,
                                  line_source="detector_line", pre_rejected=gate.rejected,
                                  ambiguous=gate.ambiguous_line, gate=gate_record)  # fmt: skip
    return ShadowFitResult(mission_id=payload.mission_id, calibrations=calibrations)
