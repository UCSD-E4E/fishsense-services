"""The remediation plan activity: one dive's rows in, its report row out.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_supersede_remediation_activities.py (the
plan half). v2 change: the activity reads nothing -- the orchestrator hands
it the dive's full population -- and so it cannot write either. The apply half
(re-plan, refuse an unplanned id, revive, log REVIVED) is the orchestrator's
and the store's: see the orchestrator's `test_remediate_laser_supersedes.py`
and the API's `test_laser_store.py`.
"""

from __future__ import annotations

from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser import PlanLaserRemediationInput
from fishsense_services_processor.laser_validation.activities import (
    plan_laser_supersede_remediation,
)

from ._laser import prod_like_dive


async def _plan(labels, **kwargs):
    return await ActivityEnvironment().run(
        plan_laser_supersede_remediation,
        PlanLaserRemediationInput(dive_id=7, labels=labels, **kwargs),
    )


async def test_the_plan_judges_the_full_population():
    labels = prod_like_dive(offsets={5: 60.0}, superseded={5, 9})

    plan = await _plan(labels)

    assert plan.dive_id == 7
    assert plan.revive_ids == [labels[9].number]
    assert plan.superseded_now == 2


async def test_the_plan_honours_exclusions():
    labels = prod_like_dive(superseded={9, 20})

    plan = await _plan(labels, excluded_label_ids=[labels[9].number])

    assert plan.revive_ids == [labels[20].number]
    assert plan.excluded_kept == [labels[9].number]


async def test_the_plan_honours_an_excluded_dive():
    plan = await _plan(prod_like_dive(superseded={9}), dive_excluded=True)

    assert plan.status == "excluded"
    assert plan.revive_ids == []


async def test_calibration_frames_are_reported():
    labels = prod_like_dive(superseded={20})

    plan = await _plan(labels, calibration_capture_numbers=[labels[20].capture_number])

    assert plan.revive_on_calibration_frames == [labels[20].number]
