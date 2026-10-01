"""`judge_dive` is the one definition of what the laser validator decides.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_dive_judgement.py. Names, bodies and reasons
are v1's; v2 adaptation: labels are `LaserLabelRow`s, whose `number` and
`capture_number` are v1's label and image ids for a migrated row, so the ids
the judgement speaks in are numbers.

Two callers act on it and must never disagree: the hourly validator, which
supersedes what it flags, and the remediation tool, which revives superseded
labels the same judgement keeps. A dive too small to fit, a line that is not
confident, a reflection split and a refused (>50%) fit all flag nothing -- and
none of them is a verdict that the labels are good.
"""

from __future__ import annotations

import numpy as np

from fishsense_services_processor.laser_validation.judgement import judge_dive

from ._laser import label, prod_like_dive, reflection_dive


def test_a_clean_confident_dive_is_judged_and_flags_its_outliers():
    labels = prod_like_dive(offsets={5: 60.0, 40: -45.0})

    judgement = judge_dive(labels, calibration_image_ids=set())

    assert judgement.judged
    assert judgement.status == "flagged"
    assert judgement.flagged_ids == {labels[5].number, labels[40].number}


def test_a_clean_dive_with_nothing_to_flag_is_still_judged():
    judgement = judge_dive(prod_like_dive(), calibration_image_ids=set())

    assert judgement.judged
    assert judgement.status == "no_outliers"
    assert judgement.flagged_ids == set()


def test_too_few_positives_is_not_a_judgement():
    judgement = judge_dive(prod_like_dive(n=4), calibration_image_ids=set())

    assert not judgement.judged
    assert judgement.status == "too_few"


def test_a_line_that_is_not_confident_is_not_a_judgement():
    """Twelve dots on two distinct pixels: core 4.1.0 gives a line whose
    inliers sit on fewer than 3 distinct pixels a confidence of 0."""
    dots = [(100.0, 100.0)] * 6 + [(200.0, 140.0)] * 6
    labels = [label(i + 1, 1000 + i, p) for i, p in enumerate(dots)]

    judgement = judge_dive(labels, calibration_image_ids=set())

    assert not judgement.judged
    assert judgement.status == "not_confident"
    assert judgement.flagged_ids == set()


def test_a_reflection_split_is_not_a_judgement():
    judgement = judge_dive(reflection_dive(), calibration_image_ids=set())

    assert not judgement.judged
    assert judgement.status == "reflection"
    assert judgement.reflection is not None


def test_the_fraction_gate_is_not_a_judgement(monkeypatch):
    from fishsense_services_processor.laser_validation import judgement as sut

    def flags_sixty_percent(xy, _fit, **_kwargs):
        flags = np.zeros(xy.shape[0], dtype=bool)
        flags[: int(0.6 * xy.shape[0])] = True
        return flags

    monkeypatch.setattr(sut, "flag_outliers", flags_sixty_percent)

    judgement = judge_dive(prod_like_dive(), calibration_image_ids=set())

    assert not judgement.judged
    assert judgement.status == "gate"
    assert judgement.flagged_ids == set()


def test_superseded_labels_are_judged_too():
    """The whole population, every run."""
    labels = prod_like_dive(offsets={5: 60.0}, superseded={5, 9})

    judgement = judge_dive(labels, calibration_image_ids=set())

    assert labels[5].number in judgement.flagged_ids
    assert labels[9].number not in judgement.flagged_ids
    assert labels[9].number in judgement.kept_ids


def test_the_verdict_does_not_depend_on_input_order():
    labels = prod_like_dive(offsets={5: 60.0, 40: -45.0})
    shuffled = list(reversed(labels))

    assert (
        judge_dive(shuffled, calibration_image_ids=set()).flagged_ids
        == judge_dive(labels, calibration_image_ids=set()).flagged_ids
    )


def test_the_order_is_capture_number_then_label_number():
    """v2: v1's (image_id, id) is (captures.number, laser_labels.number) --
    both are v1's ids for a migrated row, so a migrated dive is fitted in the
    order v1 fitted it and RANSAC settles on the line v1's did."""
    from fishsense_services_processor.laser_validation.judgement import _positives

    rows = [
        label(3, 20, (1.0, 1.0)),
        label(1, 20, (2.0, 2.0)),
        label(2, 10, (3.0, 3.0)),
    ]

    assert [r.number for r in _positives(rows)] == [2, 1, 3]


def test_calibration_frames_get_the_coarse_rule_and_are_marked():
    labels = prod_like_dive()
    on_slate = labels[30]
    on_slate.y += 8.0  # a genuine slate dot a few px off the fish line

    judgement = judge_dive(labels, calibration_image_ids={on_slate.capture_number})

    assert on_slate.number not in judgement.flagged_ids
    assert judgement.is_calibration(on_slate.number)
    assert not judgement.is_calibration(labels[0].number)


def test_labels_without_a_dot_are_not_part_of_the_population():
    labels = prod_like_dive() + [label(900, 9000, None)]

    judgement = judge_dive(labels, calibration_image_ids=set())

    assert 900 not in judgement.kept_ids | judgement.flagged_ids
