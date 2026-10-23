"""The SimCC head/tail keypoint replay's payloads (v19): read-only."""

import uuid

from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointFramesInput,
    KeypointResult,
)


def test_a_request_carries_each_frames_render_and_mask_box():
    m = uuid.uuid4()
    r = KeypointFramesInput(tenant_id=uuid.uuid4(), mission_id=m, frames=[
        KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                      render_url="u", mask_bbox=[1, 2, 30, 40])])  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)
    assert wire["dive_id"] == str(m)
    assert KeypointFramesInput.model_validate(wire).frames[0].mask_bbox == [
        1,
        2,
        30,
        40,
    ]


def test_a_result_holds_both_inputs_points():
    r = KeypointResult(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                       status="predicted", head=[1.0, 2.0], tail=[3.0, 4.0],
                       confidence=0.9, crop=[0, 0, 10, 10], rt_head=[1.1, 2.0],
                       rt_tail=[3.0, 4.1], rt_confidence=0.8)  # fmt: skip
    back = KeypointResult.model_validate(r.model_dump(mode="json"))
    assert back.rt_tail == [3.0, 4.1]
    assert KeypointResult(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                          status="render_unavailable").head is None  # fmt: skip
