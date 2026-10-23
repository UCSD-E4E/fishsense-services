"""Calibration v4's payloads (v18): the slate pose per frame on the GPU, and
the replay fitting on the points it gives."""

import uuid

from fishsense_services_contracts.automatic_results import FitAutomaticCalibrationInput
from fishsense_services_contracts.calibration_v3 import (
    CalibrationV3Report,
    ReplayCalibrationInput,
)
from fishsense_services_contracts.calibration_v4 import (
    POSE_ECC_MIN,
    SlatePose,
    SlatePoseFrame,
    SlatePoseFramesInput,
)
from fishsense_services_contracts.object_store import ObjectRef


def test_a_pose_request_carries_the_template_and_each_frames_dot():
    m = uuid.uuid4()
    r = SlatePoseFramesInput(
        tenant_id=uuid.uuid4(), mission_id=m, camera_matrix=[[1.0]],
        slate_pdf=ObjectRef(bucket="scratch", key="slate_pdf/x.pdf"), slate_dpi=200,
        reference_points=[[1.0, 2.0]],
        frames=[SlatePoseFrame(capture_id=uuid.uuid4(), render_url="u", dot=[3.0, 4.0])],
    )  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)
    assert wire["dive_id"] == str(m)
    assert SlatePoseFramesInput.model_validate(wire).frames[0].dot == [3.0, 4.0]


def test_a_pose_says_what_it_found_and_only_a_strong_alignment_is_posed():
    p = SlatePose(capture_id=uuid.uuid4(), status="weak_alignment", ecc=0.5,
                  point=[0.1, 0.0, 1.0])  # fmt: skip
    assert SlatePose.model_validate(p.model_dump(mode="json")).point == [0.1, 0.0, 1.0]
    assert 0.5 < POSE_ECC_MIN < 1.0


def test_a_replay_may_fit_on_pose_points_and_report_the_poses():
    payload = FitAutomaticCalibrationInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                           camera_matrix=[[1.0]], frames=[])  # fmt: skip
    r = ReplayCalibrationInput(payload=payload, size_source="pose",
                               pose_points={"a": [0.1, 0.0, 1.0], "b": None})  # fmt: skip
    assert r.pose_points["b"] is None
    report = CalibrationV3Report(mission_id=uuid.uuid4(), outcome="accepted",
                                 size_source="pose",
                                 poses=[SlatePose(capture_id=uuid.uuid4(), status="posed")])  # fmt: skip
    assert CalibrationV3Report.model_validate(report.model_dump(mode="json")).poses
