"""Plan the revival of laser labels the eroding validator superseded.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/
laser_label_validation/remediation.py. Pure: given a dive's labels
(superseded included) and its calibration frames, say which superseded labels
to revive. The dry run, the apply step's safety check and the tests all go
through `plan_dive`, so there is one definition of "what remediation would
do".

The judgement is `judge_dive` -- the validator's own -- so a revived label is
exactly one the next hourly validator run keeps.

v2 change: the plan is the contract's `DivePlan`, and names the dive and its
labels by `number` (v1's ids for a migrated row).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Iterable

from fishsense_services_contracts.laser import (
    DivePlan,
    LaserLabelRow,
    revival_digest,
)
from fishsense_services_processor.laser_validation.judgement import judge_dive

__all__ = ["EXCLUDED", "plan_digest", "plan_dive"]

EXCLUDED = "excluded"


def plan_dive(  # pylint: disable=too-many-arguments
    dive_id: int,
    labels: Iterable[LaserLabelRow],
    calibration_image_ids: set,
    *,
    excluded_label_ids: set | None = None,
    dive_excluded: bool = False,
) -> DivePlan:
    """What remediation would do to one dive (by number)."""
    judgement = judge_dive(list(labels), calibration_image_ids)
    positives = judgement.positives
    superseded = [label for label in positives if label.superseded]
    plan = DivePlan(
        dive_id=dive_id,
        status=EXCLUDED if dive_excluded else judgement.status,
        positives=len(positives),
        superseded_now=len(superseded),
        superseded_after=len(superseded),
    )
    if judgement.reflection is not None:
        plan.reflection_suspect = {
            key: float(value) if isinstance(value, float) else value
            for key, value in asdict(judgement.reflection).items()
        }
    if dive_excluded:
        return plan
    if not judgement.judged:
        plan.unjudged_superseded = len(superseded)
        return plan

    excluded_label_ids = excluded_label_ids or set()
    kept = [
        label
        for label in superseded
        if label.number not in judgement.flagged_ids and label.completed
    ]
    plan.excluded_kept = sorted(
        label.number for label in kept if label.number in excluded_label_ids
    )
    revive = sorted(
        (label for label in kept if label.number not in excluded_label_ids),
        key=lambda label: label.number,
    )
    plan.revive_ids = [label.number for label in revive]
    plan.superseded_after = len(superseded) - len(revive)

    live_per_image = Counter(
        label.capture_number for label in positives if not label.superseded
    )
    plan.revive_on_calibration_frames = [
        label.number for label in revive if judgement.is_calibration(label.number)
    ]
    plan.revive_on_images_with_another_live_label = [
        label.number for label in revive if live_per_image[label.capture_number] > 0
    ]
    return plan


def plan_digest(plans: Iterable[DivePlan]) -> str:
    """sha256 over exactly the revivals, independent of dive order."""
    return revival_digest((plan.dive_id, plan.revive_ids) for plan in plans)
