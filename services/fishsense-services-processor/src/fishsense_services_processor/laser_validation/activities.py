"""The light role's laser activities: the gate, the validator, the plan.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
(evaluate_laser_auto_accept_activity.py,
validate_laser_labels_for_dive_activity.py, laser_supersede_remediation.py's
plan half). The decisions and their log lines are v1's.

v2 change, the one that shapes all three: **they read and write nothing.** v1's
fetched rows through the API from the data-worker and PUT the results back;
the v2 processor may not touch the database (PLAN.md §9.11), so the
orchestrator reads the rows, these decide, and the orchestrator writes what
they return. Consequences, each owned elsewhere now:

* writing only verdicts that changed (v1's `_changed`), superseding only rows
  still live, appending the dive line only when it changed -- the store's
  (`fishsense_services_api.laser_store`);
* the apply half of remediation (re-plan, refuse an unplanned id, revive) --
  the orchestrator's, through this same plan;
* v1's heartbeat pump, which covered slow fetches: there are none here, and
  the fit is sub-second.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
from fishsense_core.laser import COARSE_CALIBRATION_TOLERANCE_PX, MIN_POINTS_FOR_LINE
from temporalio import activity

from fishsense_services_contracts.laser import (
    LASER_PREDICTOR_VERSION,
    DivePlan,
    EvaluateLaserAutoAcceptInput,
    LaserAutoAcceptResult,
    LaserAutoAcceptSummary,
    LaserFrameVerdict,
    LaserLineFit,
    LaserSupersede,
    LaserValidationResult,
    PlanLaserRemediationInput,
    ReflectionReport,
    SupersedeReason,
    ValidateLaserLabelsInput,
)
from fishsense_services_processor.laser_validation.auto_accept import (
    DiveIneligibleReason,
    FrameVerdict,
    evaluate_dive,
)
from fishsense_services_processor.laser_validation.judgement import (
    GATE,
    MAX_OUTLIER_FRACTION,
    NO_FIT,
    NO_OUTLIERS,
    NOT_CONFIDENT,
    REFLECTION,
    TOO_FEW,
    judge_dive,
)
from fishsense_services_processor.laser_validation.remediation import plan_dive
from fishsense_services_processor.laser_validation.settings import (
    LaserAutoAcceptSettings,
)

__all__ = [
    "evaluate_laser_auto_accept",
    "plan_laser_supersede_remediation",
    "validate_laser_labels_for_dive",
]


# --- the auto-accept gate -----------------------------------------------------


def _points(predictions) -> np.ndarray:
    """(N, 2) of predicted dot positions, NaN where the detector abstained --
    an abstention still needs a verdict, though it takes no part in the fit."""
    return np.array(
        [
            (
                (float(p.x), float(p.y))
                if p.x is not None and p.y is not None
                else (
                    np.nan,
                    np.nan,
                )
            )
            for p in predictions
        ],
        dtype=float,
    ).reshape(-1, 2)


def _refuse_dive(payload, reason: DiveIneligibleReason, enabled: bool):
    """Every frame ineligible, with no margins: v1's `_refuse_dive`, for a
    refusal decided before the fit. The orchestrator writes each verdict that
    differs from the standing one, which is what clears an `auto_accept` left
    from an earlier fit."""
    frames = [
        LaserFrameVerdict(
            prediction_id=p.prediction_id,
            auto_accept=False,
            gate_verdict=FrameVerdict.DIVE_INELIGIBLE.value,
        )
        for p in payload.predictions
    ]
    summary = LaserAutoAcceptSummary(
        dive_id=payload.dive_id,
        enabled=enabled,
        eligible=False,
        reason=reason.value,
        n_points=len(frames),
        auto_accepted=0,
        verdicts={FrameVerdict.DIVE_INELIGIBLE.value: len(frames)},
    )
    return LaserAutoAcceptResult(summary=summary, frames=frames)


@activity.defn(name="evaluate_laser_auto_accept")
async def evaluate_laser_auto_accept(
    payload: EvaluateLaserAutoAcceptInput,
) -> LaserAutoAcceptResult:
    """Judge the dive's WHOLE current prediction set -- the gate's safety
    argument is consensus across the dive -- and return one verdict per
    prediction and the per-dive summary."""
    dive = payload.dive_id
    activity.logger.info("dive_id=%s auto-accept gate starting", dive)
    activity.heartbeat()
    predictions = payload.predictions
    if not predictions:
        activity.logger.info("dive_id=%s has no laser predictions", dive)
        return LaserAutoAcceptResult(
            summary=LaserAutoAcceptSummary(dive_id=dive, eligible=False), frames=[]
        )

    config = LaserAutoAcceptSettings().config()

    # Refuse the WHOLE dive if ANY prediction is not from the current detector
    # (v1's rule and reasons): stage v1 and every pre-versioning row hardcoded
    # "Red Laser", and auto-accepting one writes a possibly-wrong colour with no
    # human in the loop. Whole dive, because a line fitted across two detector
    # behaviours is not a meaningful fit.
    stale = [p for p in predictions if p.predictor_version != LASER_PREDICTOR_VERSION]
    if stale:
        activity.logger.warning(
            "dive_id=%s refused: %d/%d predictions are not from detector v%d "
            "(versions present: %s); re-predict before judging",
            dive,
            len(stale),
            len(predictions),
            LASER_PREDICTOR_VERSION,
            sorted({str(p.predictor_version) for p in stale}),
        )
        return _refuse_dive(
            payload, DiveIneligibleReason.STALE_PREDICTOR, config.enabled
        )

    gate, decisions = evaluate_dive(
        payload.dive_number,
        [p.capture_number for p in predictions],
        _points(predictions),
        config=config,
    )
    counts = Counter(d.reason.value for d in decisions)
    activity.logger.info(
        "dive_id=%s gate enabled=%s eligible=%s reason=%s n=%d "
        "inliers=%d (%.0f%%) line_confidence=%.1f verdicts=%s",
        dive,
        config.enabled,
        gate.eligible,
        gate.reason.value if gate.reason else None,
        gate.n_points,
        gate.inlier_count,
        100.0 * gate.inlier_fraction,
        gate.line_confidence,
        dict(counts),
    )
    frames = [
        LaserFrameVerdict(
            prediction_id=prediction.prediction_id,
            auto_accept=decision.auto_accept,
            gate_verdict=decision.reason.value,
            line_offset_px=decision.perpendicular_px,
            line_position_z=decision.along_line_z,
        )
        for prediction, decision in zip(predictions, decisions)
    ]
    # Counted off the flag, not the histogram: with the gate disabled the two
    # disagree on purpose, and every caller wants the flag.
    accepted = sum(1 for decision in decisions if decision.auto_accept)
    return LaserAutoAcceptResult(
        summary=LaserAutoAcceptSummary(
            dive_id=dive,
            enabled=config.enabled,
            eligible=gate.eligible,
            reason=gate.reason.value if gate.reason else None,
            n_points=gate.n_points,
            inlier_count=gate.inlier_count,
            inlier_fraction=gate.inlier_fraction,
            line_confidence=gate.line_confidence,
            auto_accepted=accepted,
            verdicts=dict(counts),
        ),
        frames=frames,
    )


# --- per-dive laser-label validation ------------------------------------------


def _line(fit) -> LaserLineFit:
    return LaserLineFit(
        a=fit.a,
        b=fit.b,
        c=fit.c,
        n_points=fit.n_points,
        inlier_count=fit.inlier_count,
        inlier_fraction=fit.inlier_fraction,
        residual_std=fit.residual_std,
        label_noise_mad=fit.label_noise_mad,
        line_confidence=fit.line_confidence,
    )


@activity.defn(name="validate_laser_labels_for_dive")
async def validate_laser_labels_for_dive(
    payload: ValidateLaserLabelsInput,
) -> LaserValidationResult:
    # pylint: disable=too-many-return-statements
    #   v1's flat guard -> log -> return per verdict.
    """One judgement of the dive's FULL population (superseded included), in
    (capture number, label number) order. Returns the line whenever one was
    fitted, and the labels to supersede: flagged AND still live, each with the
    rule that took it. Never a revival -- that is the reviewed remediation
    tool's."""
    dive = payload.dive_id
    activity.heartbeat()
    labels = payload.labels
    activity.logger.info(
        "dive_id=%s validation starting; %d laser label rows", dive, len(labels)
    )
    calibration = set(payload.calibration_capture_numbers)
    activity.logger.info(
        "dive_id=%s has %d calibration frames among its slate labels; "
        "those are judged at %.0fpx rather than 3 sigma",
        dive,
        len(calibration),
        COARSE_CALIBRATION_TOLERANCE_PX,
    )

    judgement = judge_dive(labels, calibration)
    n_positives = len(judgement.positives)
    result = LaserValidationResult(
        dive_id=dive, status=judgement.status, positives=n_positives
    )
    if judgement.status == TOO_FEW:
        activity.logger.info(
            "dive_id=%s has %d positive laser labels (<%d); skipping line fit",
            dive,
            n_positives,
            MIN_POINTS_FOR_LINE,
        )
        return result
    if judgement.status == NO_FIT:
        activity.logger.info(
            "dive_id=%s: line fit returned None despite %d positives "
            "(unexpected; check inputs)",
            dive,
            n_positives,
        )
        return result

    fit = judgement.fit
    activity.logger.info(
        "dive_id=%s line fit: n=%d inliers=%d (%.0f%%) residual_std=%.2fpx "
        "label_noise_mad=%.2fpx line_confidence=%.1f confident=%s",
        dive,
        fit.n_points,
        fit.inlier_count,
        100.0 * fit.inlier_fraction,
        fit.residual_std,
        fit.label_noise_mad,
        fit.line_confidence,
        fit.is_confident,
    )
    # A byproduct already computed, recorded on every run that fits one (v1).
    # A WITHIN-dive property: never a prior for another dive.
    result.line = _line(fit)
    result.flagged = judgement.n_flagged_before_gate

    if judgement.status == REFLECTION:
        suspect = judgement.reflection
        result.reflection = ReflectionReport(
            n_primary=suspect.n_primary,
            n_secondary=suspect.n_secondary,
            separation_px=suspect.separation_px,
            angle_deg=suspect.angle_deg,
        )
        activity.logger.error(
            "dive_id=%s REFLECTION SUSPECT: laser dots form two parallel lines "
            "-- primary n=%d, secondary n=%d at %.1fpx separation (angle %.2f "
            "deg). Likely specular-reflection mislabels; single-line validation "
            "cannot resolve which is real. Skipping supersede; manual "
            "remediation required before this dive's labels feed stage 13.",
            dive,
            suspect.n_primary,
            suspect.n_secondary,
            suspect.separation_px,
            suspect.angle_deg,
        )
        return result

    if judgement.status in (NOT_CONFIDENT, NO_OUTLIERS):
        activity.logger.info("dive_id=%s: no outlier laser labels", dive)
        return result

    n_outliers = judgement.n_flagged_before_gate
    if judgement.status == GATE:
        activity.logger.warning(
            "dive_id=%s would supersede %d/%d positive laser labels (%.0f%%, "
            "gate=%.0f%%); refusing -- line fit is likely degenerate. Labels "
            "left unchanged for manual review.",
            dive,
            n_outliers,
            n_positives,
            100.0 * n_outliers / n_positives,
            100.0 * MAX_OUTLIER_FRACTION,
        )
        return result

    for label in judgement.positives:
        if label.number not in judgement.flagged_ids or label.superseded:
            continue
        coarse = judgement.is_calibration(label.number)
        activity.logger.info(
            "dive_id=%s OUTLIER laser_label_number=%d capture_number=%d "
            "x=%.1f y=%.1f perp=%.2fpx rule=%s -> superseded=True",
            dive,
            label.number,
            label.capture_number,
            float(label.x),
            float(label.y),
            judgement.perpendicular_px[label.number],
            "coarse-calibration" if coarse else "3-sigma",
        )
        result.supersede.append(
            LaserSupersede(
                label_id=label.label_id,
                reason=(
                    SupersedeReason.VALIDATOR_COARSE_CALIBRATION
                    if coarse
                    else SupersedeReason.VALIDATOR_3SIGMA
                ),
            )
        )
    if not result.supersede:
        activity.logger.info(
            "dive_id=%s: %d outlier laser labels, all already superseded",
            dive,
            n_outliers,
        )
    return result


# --- remediation --------------------------------------------------------------


@activity.defn(name="plan_laser_supersede_remediation")
async def plan_laser_supersede_remediation(
    payload: PlanLaserRemediationInput,
) -> DivePlan:
    """One dive's report row. Decides only."""
    activity.heartbeat()
    return plan_dive(
        payload.dive_id,
        payload.labels,
        set(payload.calibration_capture_numbers),
        excluded_label_ids=set(payload.excluded_label_ids),
        dive_excluded=payload.dive_excluded,
    )
