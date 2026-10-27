"""One slate frame's pose, end to end (calibration v4, 2026-10-23):

1. SAM 3.1 on the render downscaled as the shadow calibration's
   (`shadow_calibration.segment.downscaled`, 1600 px), its prompts in turn at
   its 0.3 threshold: the highest-scoring mask containing the dot, under the
   first prompt that has one (as `segment_dots` picks a size's mask);
2. the board fitted to that mask (`automatic_calibration.pose.fit_board`), in
   the camera scaled to the render;
3. the template's page aligned to the board on the **full-resolution** render
   (`align_template_both`) -- the 1600-px cache's reef alignments were weak;
   both ways up are scored and the loser's correlation is kept with the
   page's up (2026-10-25: the wrong-way-up margin, 436);
4. the plane and the dot's point on it (`slate_plane`, `dot_point`); and the
   dot on the board's own plane too (`board_point`, 2026-10-26), a range no
   orientation of the page decides.

Each step that finds nothing ends the frame with its status; a weak alignment
(under `POSE_ECC_MIN`) is posed and recorded but its point is not used.
"""

from __future__ import annotations

from typing import Any, Callable, List, Sequence, Tuple

import cv2
import numpy as np

from fishsense_services_contracts.calibration_v4 import POSE_ECC_MIN
from fishsense_services_contracts.shadow_calibration import SHADOW_SAM_PROMPTS
from fishsense_services_processor.automatic_calibration import pose
from fishsense_services_processor.shadow_calibration.segment import downscaled

__all__ = ["mask_under", "pose_frame", "render_page"]

#: ``(image RGB, prompt) -> [(mask (H, W) bool, score)]``.
Segmenter = Callable[[np.ndarray, str], List[Tuple[np.ndarray, float]]]


def render_page(pdf_bytes: bytes, dpi: int) -> np.ndarray:
    """Page 0 of the template at `dpi`, gray (the reference points' pixels)."""
    import pymupdf  # pylint: disable=import-outside-toplevel

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        pixmap = document.load_page(0).get_pixmap(dpi=dpi)
        raw = np.frombuffer(pixmap.samples, dtype=np.uint8)
        img = raw.reshape(pixmap.height, pixmap.width, pixmap.n)
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2GRAY)


def mask_under(image, pixel, segmenter: Segmenter, prompts=SHADOW_SAM_PROMPTS):
    """``(mask, prompt, score)`` of the best mask containing `pixel`, under
    the first prompt with one; ``(None, None, None)`` if none does."""
    x, y = pixel
    h, w = image.shape[:2]
    if not (0 <= y < h and 0 <= x < w):
        return None, None, None
    for prompt in prompts:
        hits = [(m, s) for m, s in segmenter(image, prompt) if m[y, x]]
        if hits:
            m, s = max(hits, key=lambda p: p[1])
            return m, prompt, float(s)
    return None, None, None


def _scaled(camera_matrix, scale: float) -> np.ndarray:
    k = np.asarray(camera_matrix, float).copy()
    k[:2] /= scale
    return k


def pose_frame(
    jpeg: bytes,
    dot: Sequence[float],
    camera_matrix,
    *,
    segmenter: Segmenter,
    page_grey: np.ndarray,
    dpi: int,
    reference_points,
) -> dict[str, Any]:
    """The frame's `SlatePose` fields (without its capture id). Blocking.
    Raises `ValueError` if the render does not decode."""
    small, scale = downscaled(jpeg)
    full = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if full is None:
        raise ValueError("the render does not decode")
    pixel = (int(round(dot[0] / scale)), int(round(dot[1] / scale)))
    mask, prompt, score = mask_under(small, pixel, segmenter)
    out: dict[str, Any] = {"sam_prompt": prompt, "sam_score": score}
    if mask is None:
        return {**out, "status": "no_mask"}
    board = pose.fit_board(mask, _scaled(camera_matrix, scale))
    if board is None:
        return {**out, "status": "no_board"}
    out.update(board_covered=board.covered, board_extra=board.extra,
               tilt_deg=pose.tilt_deg(board.rvec))  # fmt: skip
    # The dot on the board's own plane (2026-10-26): the rigid board fitted
    # to the mask knows nothing of which way up the page is, so its range is
    # a cross-check on the template's pose (436's wrong-way-up frames, which
    # nothing else recorded tells apart). The scaled camera only changes the
    # pixels, so the full-resolution dot meets the same plane.
    board_point = pose.dot_point(pose.board_plane(board), dot, camera_matrix)
    if board_point is not None:
        out["board_point"] = [float(v) for v in board_point]
    aligned = pose.align_template_both(full, board.corners * scale, page_grey, dpi)
    if aligned is None:
        return {**out, "status": "not_aligned"}
    h, ecc = aligned.h, aligned.ecc
    # Both ways up scored, and the page's up recorded (2026-10-25): the
    # margin a wrong-way-up check needs (436).
    out.update(ecc=ecc, ecc_other=aligned.ecc_other, page_up_deg=aligned.page_up_deg)
    plane = pose.slate_plane(reference_points, dpi, h, camera_matrix)
    if plane is None:
        return {**out, "status": "no_plane"}
    plane_, image_points, rms = plane
    out.update(pnp_rms_px=rms,
               reference_image_points=[[float(a), float(b)] for a, b in image_points])  # fmt: skip
    point = pose.dot_point(plane_, dot, camera_matrix)
    if point is None:
        return {**out, "status": "no_point"}
    out["point"] = [float(v) for v in point]
    return {**out, "status": "posed" if ecc >= POSE_ECC_MIN else "weak_alignment"}
