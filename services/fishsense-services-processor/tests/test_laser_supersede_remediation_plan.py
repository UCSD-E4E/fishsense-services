"""The remediation plan: which superseded laser labels to revive.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_supersede_remediation_plan.py. Names, bodies
and reasons are v1's; v2 adaptation: labels are `LaserLabelRow`s and the plan
names dives and labels by `number` (v1's ids for migrated rows), so a report
reads exactly as v1's did.

The plan proposes reviving exactly the superseded labels one full-population
judgement keeps -- and nothing the validator would supersede again -- under
constraints that are each a way a revival could do harm:

* only where the dive was actually JUDGED;
* never a label or dive on the operator's exclusion list;
* only completed labels;
* reflection suspects are reported for review, never revived.
"""

from __future__ import annotations

import json

from fishsense_services_processor.laser_validation.remediation import (
    plan_digest,
    plan_dive,
)

from ._laser import label, prod_like_dive, reflection_dive


def test_revives_eroded_labels_the_fit_keeps_and_not_the_ones_it_flags():
    labels = prod_like_dive(offsets={5: 60.0}, superseded={5, 9, 20})

    plan = plan_dive(7, labels, calibration_image_ids=set())

    assert plan.status == "flagged"
    assert plan.revive_ids == [labels[9].number, labels[20].number]
    assert plan.superseded_now == 3
    assert plan.superseded_after == 1


def test_an_unjudged_dive_revives_nothing():
    dots = [(100.0, 100.0)] * 6 + [(200.0, 140.0)] * 6
    labels = [label(i + 1, 1000 + i, p, superseded=i < 4) for i, p in enumerate(dots)]

    plan = plan_dive(7, labels, calibration_image_ids=set())

    assert plan.status == "not_confident"
    assert plan.revive_ids == []
    assert plan.superseded_after == plan.superseded_now == 4
    assert plan.unjudged_superseded == 4


def test_a_reflection_suspect_is_reported_and_never_revived():
    plan = plan_dive(
        77, reflection_dive(superseded_secondary=True), calibration_image_ids=set()
    )

    assert plan.status == "reflection"
    assert plan.revive_ids == []
    assert plan.reflection_suspect is not None
    assert plan.reflection_suspect["n_secondary"] > 0


def test_excluded_labels_are_never_revived():
    labels = prod_like_dive(superseded={9, 20})

    plan = plan_dive(7, labels, set(), excluded_label_ids={labels[9].number})

    assert plan.revive_ids == [labels[20].number]
    assert plan.excluded_kept == [labels[9].number]


def test_an_excluded_dive_revives_nothing_and_says_why():
    plan = plan_dive(7, prod_like_dive(superseded={9, 20}), set(), dive_excluded=True)

    assert plan.status == "excluded"
    assert plan.revive_ids == []
    assert plan.superseded_after == 2


def test_only_completed_labels_are_revived():
    labels = prod_like_dive(superseded={9, 20})
    labels[20].completed = False

    plan = plan_dive(7, labels, set())

    assert plan.revive_ids == [labels[9].number]


def test_revivals_needing_a_human_downstream_are_counted():
    labels = prod_like_dive(superseded={9, 20, 30})
    # 20 sits on a calibration (slate) frame; 30's capture has a second, live label.
    second = label(500, labels[30].capture_number, (labels[30].x, labels[30].y))
    labels.append(second)

    plan = plan_dive(7, labels, calibration_image_ids={labels[20].capture_number})

    assert set(plan.revive_ids) == {
        labels[9].number,
        labels[20].number,
        labels[30].number,
    }
    assert plan.revive_on_calibration_frames == [labels[20].number]
    assert plan.revive_on_images_with_another_live_label == [labels[30].number]


def test_a_live_label_is_never_in_the_plan():
    plan = plan_dive(7, prod_like_dive(), set())

    assert plan.revive_ids == []
    assert plan.superseded_now == 0


def test_the_digest_names_exactly_the_revivals_and_ignores_order():
    a = plan_dive(7, prod_like_dive(superseded={9}), set())
    b = plan_dive(8, prod_like_dive(superseded={20}, seed=1), set())

    assert plan_digest([a, b]) == plan_digest([b, a])
    assert plan_digest([a]) != plan_digest([a, b])


def test_the_report_row_is_json_ready():
    plan = plan_dive(7, prod_like_dive(offsets={5: 60.0}, superseded={5, 9}), set())

    row = json.loads(json.dumps(plan.to_dict()))
    assert row["dive_id"] == 7
    assert row["revive_ids"] == [10]
    assert row["positives"] == 80
