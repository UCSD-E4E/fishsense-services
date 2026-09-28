"""The laser-label validator's processor activity: one judgement of a dive's
full population, returned as what to write.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_validate_laser_labels_for_dive_activity.py. Names,
bodies and reasons are v1's where the behaviour is the processor's. v2
changes, pinned here:

* **the activity writes nothing.** v1's PUT each supersede and the dive line
  through the API; the v2 processor is handed the rows (superseded included)
  and the calibration frames, and returns the line and the supersedes, which
  the orchestrator writes (the API's `test_laser_store.py` pins the write:
  only still-live rows, the line appended only when it changed). So v1's
  write-concurrency and write-failure tests are the store's, and its
  heartbeat-during-a-slow-fetch test has no fetch to pump through;
* the calibration frames arrive as capture numbers, already filtered to
  completed, live slate labels by the store (v1 filtered them here);
* a legacy `superseded IS NULL` row cannot reach it: v2's column is NOT NULL.
"""

from __future__ import annotations

import logging

import numpy as np
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser import (
    SupersedeReason,
    ValidateLaserLabelsInput,
)
from fishsense_services_processor.laser_validation import judgement
from fishsense_services_processor.laser_validation.activities import (
    validate_laser_labels_for_dive,
)

from ._laser import DIVE, colinear_labels, label, label_uuid, reflection_dive


async def _validate(labels, calibration=()):
    return await ActivityEnvironment().run(
        validate_laser_labels_for_dive,
        ValidateLaserLabelsInput(
            dive_id=DIVE,
            labels=labels,
            calibration_capture_numbers=list(calibration),
        ),
    )


async def test_returns_nothing_when_no_labels():
    result = await _validate([])

    assert result.supersede == []
    assert result.line is None


async def test_returns_nothing_below_minimum_positives():
    result = await _validate(colinear_labels(4))

    assert result.status == "too_few"
    assert result.supersede == []
    assert result.line is None


async def test_clean_dive_flags_no_outliers_and_supersedes_nothing():
    result = await _validate(colinear_labels(40))

    assert result.status == "no_outliers"
    assert result.supersede == []


async def test_returns_the_line_matching_the_fit():
    result = await _validate(colinear_labels(40))  # y = 0.4*x + 100

    line = result.line
    for x in (200.0, 1200.0):
        assert abs(line.a * x + line.b * (0.4 * x + 100.0) + line.c) < 1.0
    assert abs(line.a**2 + line.b**2 - 1.0) < 1e-6
    assert line.n_points == 40
    assert line.inlier_fraction > 0.9
    assert line.line_confidence > 0
    assert line.noise_estimator == "signed_residual_mad"


async def test_outlier_label_is_flagged_and_reported(caplog):
    labels = colinear_labels(40)
    labels[5].y += 50.0

    with caplog.at_level(logging.INFO):
        result = await _validate(labels)

    assert [s.label_id for s in result.supersede] == [labels[5].label_id]
    assert any(
        "OUTLIER" in rec.getMessage()
        and f"laser_label_number={labels[5].number}" in rec.getMessage()
        for rec in caplog.records
    )


async def test_supersedes_each_flagged_outlier():
    labels = colinear_labels(60)
    for i in (3, 17, 41):
        labels[i].y += 60.0

    result = await _validate(labels)

    assert {s.label_id for s in result.supersede} == {
        labels[i].label_id for i in (3, 17, 41)
    }
    assert result.flagged == 3


async def test_rerun_after_supersede_is_a_noop():
    """A re-run judges the same full population, flags the same set, and asks
    for nothing: every flagged row is already superseded."""
    labels = colinear_labels(60)
    labels[7].y += 60.0
    first = await _validate(labels)
    assert first.supersede
    labels[7].superseded = True

    second = await _validate(labels)

    assert second.flagged == first.flagged
    assert second.supersede == []


async def test_a_majority_off_the_line_is_not_superseded():
    """18 of 30 dots scattered 50 px either side of the line: superseding the
    majority of a dive is never the answer."""
    labels = colinear_labels(30)
    for idx in range(18):
        labels[idx].y += 50.0 if idx % 2 == 0 else -50.0

    assert (await _validate(labels)).supersede == []


async def test_refuses_to_supersede_when_outlier_fraction_exceeds_safety_gate(
    monkeypatch, caplog
):
    def flags_sixty_percent(xy, _fit, **_kwargs):
        flags = np.zeros(xy.shape[0], dtype=bool)
        flags[: int(0.6 * xy.shape[0])] = True
        return flags

    monkeypatch.setattr(judgement, "flag_outliers", flags_sixty_percent)

    with caplog.at_level(logging.WARNING):
        result = await _validate(colinear_labels(30))

    assert result.status == "gate"
    assert result.supersede == []
    assert any(
        "refusing" in rec.getMessage().lower() and f"dive_id={DIVE}" in rec.getMessage()
        for rec in caplog.records
    )


async def test_reflection_split_logs_error_and_stands_down(caplog):
    """Prod dive 77: name the failure loudly and supersede neither line."""
    with caplog.at_level(logging.ERROR):
        result = await _validate(reflection_dive())

    assert result.status == "reflection"
    assert result.supersede == []
    assert result.reflection is not None
    assert any("REFLECTION SUSPECT" in rec.getMessage() for rec in caplog.records)


async def test_the_line_is_still_returned_when_the_dive_stands_down():
    """v1 wrote the dive's line on every run that fitted one, reflection and
    refusal included, before deciding anything else."""
    result = await _validate(reflection_dive())

    assert result.line is not None


# --- calibration frames are judged coarsely ---------------------------------


async def test_a_genuine_slate_dot_off_the_fish_line_is_not_superseded():
    """Dive 347's shape in miniature."""
    slate = label(900, 9000, (800.0, 0.4 * 800.0 + 106.0))

    result = await _validate(colinear_labels(60) + [slate], calibration=[9000])

    assert result.supersede == []


async def test_a_wild_slate_dot_is_still_superseded():
    slate = label(901, 9001, (800.0, 0.4 * 800.0 + 180.0))

    result = await _validate(colinear_labels(60) + [slate], calibration=[9001])

    assert [s.label_id for s in result.supersede] == [label_uuid(901)]


async def test_a_measurement_dot_the_same_distance_off_is_superseded():
    fish = label(902, 9002, (800.0, 0.4 * 800.0 + 106.0))

    result = await _validate(colinear_labels(60) + [fish])

    assert [s.label_id for s in result.supersede] == [label_uuid(902)]


async def test_each_supersede_records_which_rule_took_it():
    fish = label(906, 9006, (800.0, 0.4 * 800.0 + 40.0))
    slate = label(907, 9007, (820.0, 0.4 * 820.0 + 180.0))

    result = await _validate(colinear_labels(60) + [fish, slate], calibration=[9007])

    assert {s.label_id: s.reason for s in result.supersede} == {
        label_uuid(906): SupersedeReason.VALIDATOR_3SIGMA,
        label_uuid(907): SupersedeReason.VALIDATOR_COARSE_CALIBRATION,
    }
