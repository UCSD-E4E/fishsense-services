"""The per-dive laser-label judgement: which labels one line fit flags.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/
laser_label_validation/judgement.py. The single definition of what the
validator decides, shared by the two things that act on it -- the hourly
validator (supersedes what is flagged) and the remediation tool (revives
superseded labels the same judgement keeps). A second copy would drift, and
then a revived label is superseded again within the hour.

It judges the dive's FULL population, superseded labels included, in
(image_id, id) order -- the contract fishsense-core #88 documents: re-judging
survivors erodes a dive a pass at a time, and RANSAC's answer depends on row
order. See `test_laser_validator_does_not_erode.py`.

`judged` separates a verdict from an abstention. Too few positives, a line
that is not confident, a reflection split and a refused (>50%) fit all flag
nothing, and none of them says the labels are good.

v2 change: labels are the contract's `LaserLabelRow`s. v1's `(image_id, id)`
is `(capture_number, number)` -- both v1's ids for a migrated row, so a
migrated dive is ordered, and so fitted, exactly as v1 fitted it -- and the
ids the judgement speaks in (`flagged_ids`, `kept_ids`) are label numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List

import numpy as np
from fishsense_core.laser import (
    MIN_POINTS_FOR_LINE,
    LineFit,
    fit_dive_line,
    flag_outliers,
)

from fishsense_services_contracts.laser import LaserLabelRow
from fishsense_services_processor.laser_validation.reflection import (
    ReflectionSuspect,
    detect_reflection_split,
)

__all__ = [
    "FLAGGED",
    "GATE",
    "MAX_OUTLIER_FRACTION",
    "NO_FIT",
    "NO_OUTLIERS",
    "NOT_CONFIDENT",
    "REFLECTION",
    "TOO_FEW",
    "DiveJudgement",
    "judge_dive",
]

# Safety gate: refuse to act when more than this fraction of a dive's positive
# labels would be flagged. At >50% the line fit is more likely degenerate (a
# small accidentally-aligned cluster picked over the real majority) than the
# labelers wrong at that rate.
MAX_OUTLIER_FRACTION = 0.5

#: Verdicts. Only the last two are judgements.
TOO_FEW = "too_few"
NO_FIT = "no_fit"
NOT_CONFIDENT = "not_confident"
REFLECTION = "reflection"
GATE = "gate"
NO_OUTLIERS = "no_outliers"
FLAGGED = "flagged"


@dataclass
class DiveJudgement:  # pylint: disable=too-many-instance-attributes
    """What one fit made of a dive. Ids are label numbers."""

    status: str
    positives: List[LaserLabelRow] = field(default_factory=list)
    fit: LineFit | None = None
    reflection: ReflectionSuspect | None = None
    flagged_ids: set = field(default_factory=set)
    n_flagged_before_gate: int = 0
    perpendicular_px: dict = field(default_factory=dict)
    calibration_ids: set = field(default_factory=set)

    @property
    def judged(self) -> bool:
        """True only for a confident, un-refused fit -- a verdict, not an
        abstention."""
        return self.status in (NO_OUTLIERS, FLAGGED)

    @property
    def kept_ids(self) -> set:
        """Positives the judgement did not flag. Meaningful only if judged."""
        return {label.number for label in self.positives} - self.flagged_ids

    def is_calibration(self, label_id) -> bool:
        """Whether that label sits on a calibration (slate) frame."""
        return label_id in self.calibration_ids


def _positives(labels: Iterable[LaserLabelRow]) -> List[LaserLabelRow]:
    """Labels with both coordinates, in (capture_number, number) order.

    Sentinel rows seeded by populate and skipped annotations carry null x/y and
    are not part of the population. The order is imposed here rather than
    trusted from the store: RANSAC picks point pairs by row index, so the same
    labels in another order can settle on another line (fishsense-core measured
    one dive flagging 41-63 labels across shuffles).
    """
    return sorted(
        (label for label in labels if label.x is not None and label.y is not None),
        key=lambda label: (label.capture_number, label.number),
    )


def judge_dive(
    labels: Iterable[LaserLabelRow], calibration_image_ids: set
) -> DiveJudgement:
    # pylint: disable=too-many-return-statements
    #   One early return per verdict, in the order they are decided.
    """Fit one line through the dive's positives and flag its outliers.

    `calibration_image_ids` are the capture numbers of frames carrying a
    completed, live slate label; they are judged against the coarse absolute
    bound rather than 3 sigma (see `test_coarse_calibration_frame_supersede`).
    """
    positives = _positives(labels)
    calibration_ids = {
        label.number
        for label in positives
        if label.capture_number in calibration_image_ids
    }
    judgement = DiveJudgement(
        status=TOO_FEW, positives=positives, calibration_ids=calibration_ids
    )
    if len(positives) < MIN_POINTS_FOR_LINE:
        return judgement

    xy = np.array([(float(label.x), float(label.y)) for label in positives])
    judgement.fit = fit_dive_line(xy)
    if judgement.fit is None:
        judgement.status = NO_FIT
        return judgement

    # Before confidence: a near-even split between a laser and its reflection
    # is exactly what collapses confidence, and it needs naming, not silence.
    judgement.reflection = detect_reflection_split(xy, judgement.fit)
    if judgement.reflection is not None:
        judgement.status = REFLECTION
        return judgement

    if not judgement.fit.is_confident:
        judgement.status = NOT_CONFIDENT
        return judgement

    mask = np.array([label.number in calibration_ids for label in positives])
    flags = flag_outliers(xy, judgement.fit, calibration_mask=mask)
    judgement.n_flagged_before_gate = int(flags.sum())
    perp = judgement.fit.perpendicular_distance(xy[:, 0], xy[:, 1])
    judgement.perpendicular_px = {
        label.number: float(d) for label, d in zip(positives, perp)
    }
    if judgement.n_flagged_before_gate == 0:
        judgement.status = NO_OUTLIERS
        return judgement
    if judgement.n_flagged_before_gate / len(positives) > MAX_OUTLIER_FRACTION:
        judgement.status = GATE
        return judgement

    judgement.status = FLAGGED
    judgement.flagged_ids = {
        label.number for label, flagged in zip(positives, flags) if flagged
    }
    return judgement
