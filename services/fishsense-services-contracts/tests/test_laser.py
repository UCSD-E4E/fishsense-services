"""The laser slice's contract: versions, the gate's budget, the remediation
digest, and the payloads that cross between the orchestrator and processor.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
(test_laser_predictor_version.py, test_auto_accept_timeouts.py,
test_laser_remediation.py). Names and reasons are v1's. v2 changes, each
pinned below:

* **the gate's execution bound is 5 minutes, not 10, and the child's 30, not
  35.** v1's execution budget was sized for the data-worker's own fetch of a
  dive's predictions through the API; in v2 the processor never reads the
  database, so the orchestrator reads before the child and writes after it,
  each in its own activity. The parent's run timeout stays v1's 1 h, so the
  budget those two steps need came out of the child's;
* **payloads carry rows, not ids.** The processor may not call the API
  (PLAN.md §9.11), so the light-role stages are rows in, verdicts out;
* **remediation names dives and labels by `number`**, which is v1's id for a
  migrated row, so a report reads as v1's did.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from fishsense_services_contracts import laser as sut
from fishsense_services_contracts.laser_region import LASER_REGION_POLYGON
from fishsense_services_contracts.object_store import ObjectRef

# --- the stage version (test_laser_predictor_version.py) --------------------


def test_version_is_a_positive_int():
    assert isinstance(sut.LASER_PREDICTOR_VERSION, int)
    assert sut.LASER_PREDICTOR_VERSION >= 1


def test_the_region_the_stage_gates_on_is_pinned_to_this_version():
    """Change the region and predictions that used to be accepted are
    rejected: bump the version in the same commit and update this fixture."""
    assert LASER_REGION_POLYGON == [
        [1580, 570],
        [1700, 465],
        [2335, 395],
        [2455, 525],
        [2470, 1610],
        [2185, 1890],
        [1920, 1905],
        [1625, 1365],
    ], "the laser region changed -- bump LASER_PREDICTOR_VERSION and this fixture"
    assert sut.LASER_PREDICTOR_VERSION == 2


def test_the_ls_tag_carries_the_version():
    """The pre-annotation a labeler sees and the backfill's idempotency check
    both key on this string."""
    assert sut.laser_model_version_tag() == (
        f"laser-detector-v{sut.LASER_PREDICTOR_VERSION}"
    )
    assert sut.laser_model_version_tag(1) != sut.laser_model_version_tag(2)


def test_the_tag_is_not_the_old_bare_constant():
    assert sut.laser_model_version_tag() != "laser-detector"


@pytest.mark.parametrize("version", [1, 2, 17])
def test_tag_is_stable_for_a_given_version(version):
    assert sut.laser_model_version_tag(version) == sut.laser_model_version_tag(version)


# --- the gate's budget (test_auto_accept_timeouts.py) -----------------------


def test_the_activity_budget_is_queue_wait_plus_execution():
    """`schedule_to_close` is the sum: anything less lets a long queue wait
    eat into the execution budget and cut a fit off mid-run."""
    assert (
        sut.GATE_ACTIVITY_TIMEOUT
        == sut.GATE_QUEUE_WAIT_TIMEOUT + sut.GATE_EXECUTION_TIMEOUT
    )


def test_the_queue_wait_outlasts_the_value_that_failed_in_prod():
    """15 minutes died twice on 2026-09-04 behind multi-GB rawpy decodes."""
    assert sut.GATE_QUEUE_WAIT_TIMEOUT > timedelta(minutes=15)


def test_the_execution_bound_is_tight_and_holds_no_database_reads():
    """v2 change: 5 minutes, not v1's 10. v1's bound covered the data-worker's
    fetch of the dive's predictions over Traefik; the v2 processor is handed
    them (it never reads the database), and the fit is sub-second, so a run
    still going after five minutes is stuck, not slow."""
    assert sut.GATE_EXECUTION_TIMEOUT == timedelta(minutes=5)


def test_the_child_outlives_the_activity_it_runs():
    """So the activity's own timeout fires and names the bound, rather than an
    opaque ChildWorkflowError."""
    assert sut.GATE_CHILD_EXECUTION_TIMEOUT > sut.GATE_ACTIVITY_TIMEOUT


# --- the remediation digest (test_laser_remediation.py) ---------------------


def test_the_digest_ignores_order():
    assert sut.revival_digest([(8, [3, 1]), (7, [5])]) == sut.revival_digest(
        [(7, [5]), (8, [1, 3])]
    )


def test_the_digest_ignores_dives_with_nothing_to_revive():
    assert sut.revival_digest([(7, [5]), (9, [])]) == sut.revival_digest([(7, [5])])


def test_the_digest_changes_with_any_revival():
    base = sut.revival_digest([(7, [5])])
    assert sut.revival_digest([(7, [5, 6])]) != base
    assert sut.revival_digest([(8, [5])]) != base
    assert sut.revival_digest([]) != base


def test_the_digest_is_v1s_for_the_same_numbers():
    """A migrated dive's number is its v1 id, so a digest v1 printed for a
    reviewed report is the one v2 computes for the same revivals."""
    import hashlib
    import json

    expected = hashlib.sha256(json.dumps([[7, [5, 9]]]).encode()).hexdigest()
    assert sut.revival_digest([(7, [9, 5])]) == expected


def test_dry_run_is_the_default():
    request = sut.RemediateLaserSupersedesInput(dive_ids=[7])
    assert request.apply is False
    assert request.expected_plan_sha256 is None


# --- payloads ---------------------------------------------------------------


def _ref(key="tenants/00000000-0000-0000-0000-000000000001/raw/abc.ORF"):
    return ObjectRef(bucket="fishsense-lite", key=key)


def test_preprocess_input_carries_refs_not_checksums():
    """Only the orchestrator issues keys (PLAN.md §9.11)."""
    payload = sut.PreprocessLaserImagesInput(
        dive_id=uuid.uuid4(),
        images=[
            sut.LaserPreprocessImage(
                capture_id=uuid.uuid4(),
                raw=_ref(),
                jpeg=_ref("fishsense-lite/preprocess_jpeg/abc.JPG"),
            )
        ],
        camera_matrix=[[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]],
        distortion_coefficients=[0.0] * 5,
        bbox=[1580, 395, 2470, 1905],
        laser_region=LASER_REGION_POLYGON,
    )
    assert (
        sut.PreprocessLaserImagesInput.model_validate_json(payload.model_dump_json())
        == payload
    )


def test_a_bbox_is_four_numbers():
    with pytest.raises(ValidationError):
        sut.PreprocessLaserImagesInput(
            dive_id=uuid.uuid4(),
            images=[],
            camera_matrix=[[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]],
            distortion_coefficients=[0.0] * 5,
            bbox=[1, 2, 3],
        )


def test_a_prediction_result_has_both_coordinates_or_neither():
    """The schema's own rule (laser_predictions_dot_check), checked where the
    processor's output first arrives."""
    with pytest.raises(ValidationError):
        sut.LaserPredictionResult(
            capture_id=uuid.uuid4(),
            x=1.0,
            y=None,
            confidence=0.5,
            predictor_version=2,
        )


def test_a_colour_is_red_or_green():
    with pytest.raises(ValidationError):
        sut.LaserPredictionResult(
            capture_id=uuid.uuid4(),
            x=1.0,
            y=2.0,
            confidence=0.5,
            color="blue",
            predictor_version=2,
        )


def test_a_label_row_round_trips():
    row = sut.LaserLabelRow(
        label_id=uuid.uuid4(),
        number=12,
        capture_number=345,
        x=1.5,
        y=None,
        superseded=False,
        completed=True,
    )
    assert sut.LaserLabelRow.model_validate_json(row.model_dump_json()) == row


def test_a_supersede_reason_is_one_the_validator_writes():
    """The validator writes only its two reasons; `manual` and `remediation`
    are other writers' (migration 0017)."""
    with pytest.raises(ValidationError):
        sut.LaserSupersede(label_id=uuid.uuid4(), reason="manual")
    assert {r.value for r in sut.SupersedeReason} == {
        "validator_3sigma",
        "validator_coarse_calibration",
    }


def test_the_noise_estimator_is_signed_residual_mad():
    """fishsense-core 4.1.0's; a line written with it says so (migration 0017)."""
    line = sut.LaserLineFit(
        a=0.0,
        b=1.0,
        c=-10.0,
        n_points=5,
        inlier_count=5,
        inlier_fraction=1.0,
        residual_std=0.1,
        label_noise_mad=0.1,
        line_confidence=10.0,
    )
    assert line.noise_estimator == "signed_residual_mad"


def test_a_gate_verdict_is_one_the_schema_accepts():
    with pytest.raises(ValidationError):
        sut.LaserFrameVerdict(
            prediction_id=uuid.uuid4(), auto_accept=False, gate_verdict="maybe"
        )
