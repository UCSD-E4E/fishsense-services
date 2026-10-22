"""The shadow calibration's processor steps: SAM on each slate frame, then the fit.

Segmentation follows cscw's sam_sizes.py exactly (§7.2): the render
downscaled to fit a 1600-px box (PIL LANCZOS, JPEG q95, as the port's render
cache), each dot mapped to ``int(round(dot * scale))``, the prompts "slate"
then "white board" tried in order, and under each dot the highest-scoring
mask containing it; its size is sqrt(area). SAM runs at the port's 0.3
threshold on a processor of its own over the shared model, so the head/tail
stages' 0.5 is never touched.

The fit step runs both dot sources and both variants, and fills every stored
field; on the pool its `port` rows are the published ones.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fishsense_services_contracts.shadow_calibration import (
    FitShadowCalibrationInput,
    SHADOW_SAM_PROMPTS,
    ShadowFitFrame,
)
from fishsense_services_processor.shadow_calibration.run import run_shadow_fit
from fishsense_services_processor.shadow_calibration.segment import (
    downscaled,
    segment_dots,
)

POOL = json.loads((Path(__file__).parent / "fixtures/shadow_pool.json").read_text())
SESSIONS = {s["session"]: s for s in POOL["sessions"]}


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), 90, np.uint8)).save(buf, format="JPEG")
    return buf.getvalue()


def test_a_render_is_downscaled_to_the_ports_1600_px_box():
    image, scale = downscaled(_jpeg(4014, 3016))
    assert image.shape[:2] == (1202, 1600)
    assert scale == pytest.approx(4014 / 1600)


class _Segmenter:
    """Masks per prompt; records the prompts asked."""

    def __init__(self, by_prompt):
        self.by_prompt = by_prompt
        self.asked = []

    def __call__(self, image, prompt):
        self.asked.append(prompt)
        return self.by_prompt.get(prompt, [])


def _mask(box, shape=(1202, 1600)):
    m = np.zeros(shape, bool)
    x0, y0, x1, y1 = box
    m[y0:y1, x0:x1] = True
    return m


def test_under_each_dot_the_best_mask_of_the_first_prompt_that_holds_it():
    scale = 4014 / 1600
    on_slate = (800 * scale, 600 * scale)  # inside both "white board" masks
    nowhere = (10 * scale, 10 * scale)
    seg = _Segmenter({
        "slate": [(_mask((0, 0, 50, 50)), 0.9)],  # not under the slate dot
        "white board": [(_mask((700, 500, 900, 700)), 0.6),
                        (_mask((750, 550, 850, 650)), 0.8)],
    })  # fmt: skip

    (slate, edge) = segment_dots(np.zeros((1202, 1600, 3), np.uint8), scale,
                                 [list(on_slate), list(nowhere)], seg)  # fmt: skip

    assert seg.asked == list(SHADOW_SAM_PROMPTS)
    assert (slate.prompt, slate.score, slate.mask_area_px) == (
        "white board",
        0.8,
        100 * 100,
    )
    assert slate.size == pytest.approx(100.0)
    assert (edge.prompt, edge.size) == ("slate", pytest.approx(50.0))


def test_a_dot_no_mask_holds_has_no_size():
    (d,) = segment_dots(np.zeros((1202, 1600, 3), np.uint8), 1.0, [[5.0, 5.0]],
                        _Segmenter({}))  # fmt: skip
    assert d.size is None and d.prompt is None


def test_the_dot_pixel_is_rounded_as_the_port_rounds_it():
    """sam_sizes.py: ``x, y = int(round(dot_x * SCALE)), int(round(dot_y * SCALE))``."""
    m = np.zeros((1202, 1600), bool)
    m[600, 800] = True  # one pixel
    (d,) = segment_dots(np.zeros((1202, 1600, 3), np.uint8), 2.0, [[1601.0, 1199.2]],
                        _Segmenter({"slate": [(m, 0.5)]}))  # fmt: skip
    assert d.mask_area_px == 1  # (1601/2, 1199.2/2) rounds to (800, 600)


# -- the fit step ------------------------------------------------------------------


def _payload(session, *, reference=True, line=True):
    frames = []
    for f in session["frames"]:
        frames.append(ShadowFitFrame(
            capture_id=uuid.UUID(int=f["image_id"]), frame_source="label",
            dot_x=f["dot_x"], dot_y=f["dot_y"], dot_label_source="human",
            sam_prompt=f["sam_prompt"], sam_score=f["sam_score"],
            mask_area_px=None if f["size"] is None else f["size"] ** 2, size=f["size"],
        ))  # fmt: skip
    dots = {d["image_id"]: d for d in session["detector_dots"]}
    detector = [
        ShadowFitFrame(capture_id=uuid.UUID(int=f["image_id"]), frame_source="label",
                       dot_x=dots[f["image_id"]]["x"], dot_y=dots[f["image_id"]]["y"],
                       dot_confidence=dots[f["image_id"]]["confidence"],
                       sam_prompt=f["sam_prompt"], sam_score=f["sam_score"], size=f["size"])
        for f in session["frames"] if f["image_id"] in dots
    ]  # fmt: skip
    u = np.array(session["line"]["u"])
    n = np.array([-u[1], u[0]])
    return FitShadowCalibrationInput(
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), mission_number=session["session"],
        camera_matrix=session["camera_matrix"],
        # The line as the database holds it: n . (x, y) = offset.
        mission_line=[float(n[0]), float(n[1]), -session["line"]["offset"]] if line else None,
        reference_position=session["stored_laser_position"] if reference else None,
        reference_axis=session["stored_laser_axis"] if reference else None,
        frames={"labels": frames, "detector": detector},
        mission_detector_dots=[[d["x"], d["y"], d["confidence"]] for d in session["detector_dots"]],
        render_scale=4014 / 1600, sam_model="sam3/3.1@test",
    )  # fmt: skip


def _by(result):
    return {(c.dot_source, c.variant): c for c in result.calibrations}


@pytest.mark.parametrize("session", [62, 77, 114])
def test_each_mission_gets_both_sources_and_both_variants(session):
    result = _by(run_shadow_fit(_payload(SESSIONS[session])))
    assert set(result) == {("labels", "port"), ("labels", "gated"),
                           ("detector", "port"), ("detector", "gated")}  # fmt: skip
    port = result[("labels", "port")]
    published = SESSIONS[session]["published"]["sam3_all"]
    assert port.vp_offset_deg_f2850 == pytest.approx(
        published["err_deg"], abs=5.000001e-6
    )
    assert port.angle_diff_deg == pytest.approx(published["angle_deg"], abs=5.000001e-6)
    assert port.line_source == "mission_line"
    for c in result.values():
        assert c.sam_prompts == list(SHADOW_SAM_PROMPTS) and c.sam_threshold == 0.3
        assert c.render_long_side == 1600 and c.o_mag_m == 0.104
        assert c.algorithm_version == "1" and c.code_version
        if c.outcome == "accepted":
            assert len(c.laser_position) == len(c.laser_axis) == 3
            assert c.frames_used == sum(f.used for f in c.frames)
    assert result[("detector", "gated")].outcome == (
        "refused" if session == 77 else "accepted"
    )
    assert result[("detector", "gated")].line_source == "detector_line"


def test_frames_say_why_they_were_not_used():
    result = _by(run_shadow_fit(_payload(SESSIONS[114])))
    labels = result[("labels", "gated")]
    reasons = {f.rejected_reason for f in labels.frames if not f.used}
    assert reasons == {"no_mask_at_dot"}  # 114's five frames SAM held no mask under
    assert labels.frames_rejected == {"no_mask_at_dot": 5}
    detector = result[("detector", "gated")]
    assert {f.rejected_reason for f in detector.frames if not f.used} >= {"off_line"}
    assert (
        detector.gate["off_line_px"] == 10.0
        and detector.gate["rejected"]["off_line"] == 1
    )


def test_with_no_reference_the_fit_still_runs_and_names_no_difference():
    c = _by(run_shadow_fit(_payload(SESSIONS[62], reference=False)))[
        ("labels", "gated")
    ]
    assert c.outcome == "accepted"
    assert c.angle_diff_deg is None and c.vp_offset_px is None
    assert c.lever_m_production_offset is None


def test_with_no_mission_line_the_label_source_fits_its_own_dots():
    c = _by(run_shadow_fit(_payload(SESSIONS[62], line=False)))[("labels", "port")]
    assert c.line_source == "dots" and c.outcome == "accepted"


def test_the_port_variant_is_ungated_on_detector_dots_too():
    """§7.4: `port` is the script exactly -- no gate, no trim, no refusal. On
    77 the gate rejects every slate dot; the port still fits them all."""
    result = _by(run_shadow_fit(_payload(SESSIONS[77])))
    port, gated = result[("detector", "port")], result[("detector", "gated")]
    assert port.outcome == "accepted" and port.gate == {}
    assert not any(f.rejected_reason == "off_line" for f in port.frames)
    assert gated.outcome == "refused" and gated.frames_rejected["off_line"] == 22
