"""The auto-accept gate's processor activity: a dive's predictions in, its
verdicts out.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_evaluate_laser_auto_accept_activity.py. Names and
reasons are v1's. v2 changes, pinned here:

* **the activity reads and writes nothing.** v1's fetched the dive's
  predictions and PUT the changed verdicts through the API from the
  data-worker; the v2 processor may not call the API (PLAN.md §9.11), so it is
  handed the dive's current predictions and returns one verdict per
  prediction. Writing only what changed (v1's `_changed`) is the
  orchestrator's now -- see the API's `test_laser_store.py`;
* the config comes from the processor's settings
  (``FISHSENSE_LASER_AUTO_ACCEPT_*``), v1's `laser_auto_accept.*` section;
* the audit sample is keyed on the dive's and capture's `number`.
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from pydantic import ValidationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser import (
    LASER_PREDICTOR_VERSION,
    EvaluateLaserAutoAcceptInput,
    GatePrediction,
)
from fishsense_services_processor.laser_validation.activities import (
    evaluate_laser_auto_accept,
)
from fishsense_services_processor.laser_validation.settings import (
    LaserAutoAcceptSettings,
)

DIVE = uuid.uuid4()


def _prediction(i, xy, version=LASER_PREDICTOR_VERSION):
    return GatePrediction(
        prediction_id=uuid.uuid5(uuid.NAMESPACE_OID, f"p{i}"),
        capture_number=1000 + i,
        x=None if xy is None else float(xy[0]),
        y=None if xy is None else float(xy[1]),
        predictor_version=version,
    )


def _on_line(n, *, version=LASER_PREDICTOR_VERSION):
    """`n` predictions on one line -- the shape of a healthy dive."""
    rng = np.random.default_rng(1)
    x = 800.0 + 30.0 * np.arange(n)
    y = 500.0 + 0.4 * x + rng.normal(0, 0.4, n)
    return [_prediction(i, (a, b), version) for i, (a, b) in enumerate(zip(x, y))]


def _scattered(n):
    """`n` predictions that agree on nothing -- the v1-detector shape."""
    rng = np.random.default_rng(2)
    return [
        _prediction(i, xy)
        for i, xy in enumerate(rng.uniform([500, 400], [3500, 2500], size=(n, 2)))
    ]


@pytest.fixture(autouse=True)
def no_audit(monkeypatch):
    """Geometry tests aren't perturbed by a frame being diverted at random."""
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_AUDIT_SAMPLE_RATE", "0")


async def _run(predictions, *, dive_number=7):
    return await ActivityEnvironment().run(
        evaluate_laser_auto_accept,
        EvaluateLaserAutoAcceptInput(
            dive_id=DIVE, dive_number=dive_number, predictions=predictions
        ),
    )


def _by_capture(result, predictions):
    by_id = {f.prediction_id: f for f in result.frames}
    return {p.capture_number: by_id[p.prediction_id] for p in predictions}


async def test_no_predictions_judges_nothing():
    result = await _run([])

    assert result.frames == []
    assert result.summary.dive_id == DIVE
    assert not result.summary.eligible


async def test_healthy_dive_auto_accepts_its_predictions():
    predictions = _on_line(40)

    result = await _run(predictions)

    assert result.summary.eligible
    assert result.summary.auto_accepted == 40
    assert all(f.auto_accept for f in result.frames)
    assert {f.gate_verdict for f in result.frames} == {"auto_accepted"}
    assert all(f.line_offset_px is not None for f in result.frames)


async def test_dive_without_consensus_auto_accepts_nothing():
    """The v1 shape. Every frame routes to a human, and the summary says why."""
    result = await _run(_scattered(40))

    assert not result.summary.eligible
    assert result.summary.reason == "weak_consensus"
    assert result.summary.auto_accepted == 0
    assert {f.gate_verdict for f in result.frames} == {"dive_ineligible"}


async def test_off_line_prediction_is_not_auto_accepted():
    predictions = _on_line(40)
    predictions[12] = _prediction(12, (predictions[12].x, predictions[12].y + 80.0))

    frames = _by_capture(await _run(predictions), predictions)

    assert frames[1012].gate_verdict == "off_line"
    assert not frames[1012].auto_accept
    assert frames[1012].line_offset_px > 10.0


async def test_a_dive_that_loses_consensus_returns_verdicts_that_clear_standing_ones():
    """The safety case: a re-predicted dive can lose the consensus it had.
    Every frame comes back not auto-accepted, so an `auto_accept` left standing
    from an earlier fit is overwritten by the orchestrator's write."""
    result = await _run(_scattered(40))

    assert all(not f.auto_accept for f in result.frames)
    assert all(f.line_offset_px is None for f in result.frames)


async def test_abstentions_are_judged_but_never_auto_accepted():
    predictions = _on_line(40)
    predictions[3] = _prediction(3, None)

    frames = _by_capture(await _run(predictions), predictions)

    assert frames[1003].gate_verdict == "no_prediction"
    assert not frames[1003].auto_accept


async def test_summary_verdict_counts_cover_every_prediction():
    predictions = _on_line(40)
    predictions[3] = _prediction(3, None)

    summary = (await _run(predictions)).summary

    assert sum(summary.verdicts.values()) == 40
    assert summary.verdicts["no_prediction"] == 1


async def test_a_disabled_gate_records_verdicts_but_accepts_nothing(monkeypatch):
    """The dark run: every verdict and margin still comes back."""
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_ENABLED", "false")

    result = await _run(_on_line(40))

    assert not any(f.auto_accept for f in result.frames)
    assert {f.gate_verdict for f in result.frames} == {"auto_accepted"}
    assert all(f.line_position_z is not None for f in result.frames)
    assert result.summary.enabled is False


async def test_summary_auto_accepted_counts_the_flag_not_the_verdict(monkeypatch):
    """These disagree exactly when the gate is off, and callers want the flag:
    the parent uses it to decide whether to walk the dive's tasks at all."""
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_ENABLED", "false")

    summary = (await _run(_on_line(40))).summary

    assert summary.verdicts["auto_accepted"] == 40
    assert summary.auto_accepted == 0


def test_config_is_read_from_settings():
    assert LaserAutoAcceptSettings().config().min_predictions == 20


def test_the_switch_is_settable_without_a_code_change(monkeypatch):
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_ENABLED", "false")
    assert LaserAutoAcceptSettings().config().enabled is False


def test_thresholds_are_settable_and_still_validated(monkeypatch):
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_MIN_PREDICTIONS", "3")
    with pytest.raises((ValueError, ValidationError)):
        LaserAutoAcceptSettings().config()


def test_settings_values_are_coerced(monkeypatch):
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_MIN_INLIER_FRACTION", "0.8")
    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_MAX_PERPENDICULAR_PX", "12")
    config = LaserAutoAcceptSettings().config()
    assert config.min_inlier_fraction == 0.8
    assert config.max_perpendicular_px == 12.0


# --- the stale-predictor refusal ---------------------------------------------


async def test_a_dive_with_a_pre_versioning_prediction_is_refused():
    """NULL version -- every row written before the stage was versioned."""
    predictions = _on_line(40)
    predictions[0] = _prediction(0, (predictions[0].x, predictions[0].y), None)

    result = await _run(predictions)

    assert result.summary.reason == "stale_predictor"
    assert result.summary.auto_accepted == 0
    assert {f.gate_verdict for f in result.frames} == {"dive_ineligible"}


async def test_a_dive_with_an_older_detector_version_is_refused():
    """Explicit v1, not just NULL: the version that got the colour wrong."""
    result = await _run(_on_line(40, version=1))

    assert result.summary.reason == "stale_predictor"
    assert not any(f.auto_accept for f in result.frames)


async def test_the_whole_dive_is_refused_not_just_the_stale_frames():
    """A line fitted across two detector behaviours is not a meaningful fit."""
    predictions = _on_line(40)
    predictions[39] = _prediction(39, (predictions[39].x, predictions[39].y), 1)

    result = await _run(predictions)

    assert all(f.gate_verdict == "dive_ineligible" for f in result.frames)
    assert all(
        f.line_offset_px is None and f.line_position_z is None for f in result.frames
    )
    assert result.summary.verdicts == {"dive_ineligible": 40}


async def test_an_all_current_dive_is_unaffected():
    result = await _run(_on_line(40))

    assert result.summary.reason is None
    assert result.summary.eligible


async def test_the_audit_sample_is_keyed_on_numbers(monkeypatch):
    """v2: the dive's and capture's numbers are v1's ids for a migrated dive,
    so a re-judged migrated dive audits the frames v1 audited."""
    from fishsense_services_processor.laser_validation.auto_accept import (
        _is_audit_sample,
    )

    monkeypatch.setenv("FISHSENSE_LASER_AUTO_ACCEPT_AUDIT_SAMPLE_RATE", "0.1")
    predictions = _on_line(200)

    frames = _by_capture(await _run(predictions, dive_number=442), predictions)

    audited = {n for n, f in frames.items() if f.gate_verdict == "audit_sample"}
    assert audited == {
        p.capture_number
        for p in predictions
        if _is_audit_sample(442, p.capture_number, 0.1)
    }
    assert audited
