"""One slate frame posed end to end (calibration v4): SAM's mask under the
dot on the downscaled render, the board, the pattern on the full-resolution
render, the plane, the dot's point -- against a synthetic slate whose pose and
laser point are known. A fake SAM returns the board's own mask."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf

from fishsense_services_processor.slate_pose import frame as pf
from fishsense_services_contracts.calibration_v4 import POSE_ECC_MIN

from . import test_calibration_v4_pose as synth

BODY_DOT = np.array([0.2, 0.07, 0.0])


def _scene(blank=False, noise=0.0):
    rv, tv = synth._pose(18.0)
    img, page, refs, _, _ = synth._render(rv, tv)
    if noise:
        rng = np.random.default_rng(1)
        img = np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    if blank:
        img = np.full(synth.SHAPE, 40, np.uint8)
        img[synth._mask(rv, tv)] = 245
    r = cv2.Rodrigues(rv)[0]
    target = r @ BODY_DOT + tv
    dot = (synth.K @ target)[:2] / target[2]
    jpeg = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_GRAY2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 97])[1].tobytes()  # fmt: skip
    return jpeg, dot, page, refs, target, (rv, tv)


def _sam(pose_, prompts_with_mask=("slate",)):
    def segment(image, prompt):
        if prompt not in prompts_with_mask:
            return []
        full = synth._mask(*pose_).astype(np.uint8)
        m = cv2.resize(
            full, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST
        )
        return [(m.astype(bool), 0.9), (np.zeros_like(m, bool), 0.95)]

    return segment


def test_a_slate_frame_is_posed_and_its_dot_put_on_the_slate():
    jpeg, dot, page, refs, target, pose_ = _scene()
    out = pf.pose_frame(jpeg, dot, synth.K, segmenter=_sam(pose_), page_grey=page,
                        dpi=synth.DPI, reference_points=refs)  # fmt: skip
    assert out["status"] == "posed", out
    assert out["sam_prompt"] == "slate" and out["ecc"] > POSE_ECC_MIN
    assert abs(out["tilt_deg"] - 18.0) < 4.0
    # the point within 0.3% of its range
    assert np.linalg.norm(np.array(out["point"]) - target) < 0.003 * target[2]
    assert len(out["reference_image_points"]) == len(refs)
    # Both orientations scored, and the page's up recorded (2026-10-25).
    assert out["ecc_other"] < out["ecc"]
    assert -180.0 <= out["page_up_deg"] <= 180.0
    # The dot on the board's own plane (2026-10-26): the rigid board fitted
    # to SAM's mask, which no orientation of the page decides -- a second
    # range for the dot, within 2% of the truth here.
    board = np.array(out["board_point"])
    assert abs(board[2] - target[2]) < 0.02 * target[2]


def test_the_second_prompt_is_tried_when_the_first_has_no_mask():
    jpeg, dot, page, refs, _, pose_ = _scene()
    out = pf.pose_frame(jpeg, dot, synth.K, segmenter=_sam(pose_, ("white board",)),
                        page_grey=page, dpi=synth.DPI, reference_points=refs)  # fmt: skip
    assert out["sam_prompt"] == "white board" and out["status"] == "posed"


def test_no_mask_under_the_dot_is_said_so():
    jpeg, dot, page, refs, _, pose_ = _scene()
    out = pf.pose_frame(jpeg, dot, synth.K, segmenter=_sam(pose_, ()), page_grey=page,
                        dpi=synth.DPI, reference_points=refs)  # fmt: skip
    assert out["status"] == "no_mask" and out.get("point") is None


def test_a_board_whose_pattern_does_not_align_is_not_posed_but_recorded():
    jpeg, dot, page, refs, _, pose_ = _scene(blank=True)
    out = pf.pose_frame(jpeg, dot, synth.K, segmenter=_sam(pose_), page_grey=page,
                        dpi=synth.DPI, reference_points=refs)  # fmt: skip
    assert out["status"] in ("weak_alignment", "not_aligned")
    assert out["status"] != "posed"


def test_a_weak_alignment_is_recorded_with_its_point_but_not_posed():
    """Correlation falls with image noise even where the alignment holds
    (here < 1 px at 0.67): the point is kept for the replay's report, the
    gate decides only whether the fit uses it."""
    jpeg, dot, page, refs, target, pose_ = _scene(noise=200.0)
    out = pf.pose_frame(jpeg, dot, synth.K, segmenter=_sam(pose_), page_grey=page,
                        dpi=synth.DPI, reference_points=refs)  # fmt: skip
    assert out["status"] == "weak_alignment" and out["ecc"] < POSE_ECC_MIN
    assert np.linalg.norm(np.array(out["point"]) - target) < 0.01 * target[2]


def test_the_template_page_renders_at_its_dpi():
    page, _ = synth._page()
    doc = pymupdf.open()
    p = doc.new_page(width=8.5 * 72, height=11 * 72)
    png = cv2.imencode(".png", page)[1].tobytes()
    p.insert_image(p.rect, stream=png)
    grey = pf.render_page(doc.tobytes(), synth.DPI)
    assert grey.shape == page.shape and grey.dtype == np.uint8
    assert np.mean(np.abs(grey.astype(int) - page)) < 20
