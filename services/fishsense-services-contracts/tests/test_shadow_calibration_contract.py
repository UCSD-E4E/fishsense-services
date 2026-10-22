"""The shadow calibration's payloads (v13): the port's SAM settings, pinned."""

import uuid

from fishsense_services_contracts.shadow_calibration import (
    SHADOW_ALGORITHM_VERSION,
    SHADOW_RENDER_LONG_SIDE,
    SHADOW_SAM_PROMPTS,
    SHADOW_SAM_THRESHOLD,
    SegmentSlateFramesInput,
    SlateSegment,
)


def test_the_sam_settings_are_the_ports():
    """sam_sizes.py: prompts "slate" then "white board", threshold 0.3, on a
    1600-px rectified render (cscw-fishsense2027@8312780a)."""
    assert SHADOW_SAM_PROMPTS == ("slate", "white board")
    assert SHADOW_SAM_THRESHOLD == 0.3
    assert SHADOW_RENDER_LONG_SIDE == 1600
    assert SHADOW_ALGORITHM_VERSION == "1"


def test_a_frame_names_its_render_and_its_dots_and_the_old_wire_name_parses():
    payload = SegmentSlateFramesInput.model_validate(
        {"tenant_id": str(uuid.uuid4()), "dive_id": str(uuid.uuid4()),
         "frames": [{"capture_id": str(uuid.uuid4()), "render_url": "u",
                     "dots": [[1.0, 2.0], [3.0, 4.0]]}]}  # fmt: skip
    )
    assert payload.frames[0].dots == [[1.0, 2.0], [3.0, 4.0]]
    assert SlateSegment(capture_id=uuid.uuid4(), status="render_unavailable").dots == []
