"""Pure-logic tests for the per-image transform that draws the cluster index
and encodes the rectified image to JPEG bytes.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_overlay_and_encode_jpeg.py. Names, bodies and
reasons are v1's: the unit-testable core of stage 2 -- no I/O, no Temporal, no
fishsense-core decoding.
"""

import cv2
import numpy as np

from fishsense_services_processor.species.activities import overlay_and_encode_jpeg


def _decode_jpeg(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert img is not None, "cv2.imdecode returned None — JPEG bytes invalid"
    return img


def _solid_bgr(height: int, width: int, color=(180, 180, 180)) -> np.ndarray:
    """Solid-color BGR uint8 image. Mid-gray by default so red text is
    visible against it without saturating the channel."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :, 0] = color[0]
    img[:, :, 1] = color[1]
    img[:, :, 2] = color[2]
    return img


def test_returns_non_empty_jpeg_bytes():
    img = _solid_bgr(1080, 1920)

    out = overlay_and_encode_jpeg(img, cluster_index=1, cluster_size=5)

    assert isinstance(out, bytes)
    assert len(out) > 0
    # JPEG SOI marker
    assert out[:2] == b"\xff\xd8"


def test_decoded_jpeg_has_same_shape_as_input():
    img = _solid_bgr(1080, 1920)

    out = overlay_and_encode_jpeg(img, cluster_index=1, cluster_size=5)
    decoded = _decode_jpeg(out)

    assert decoded.shape == img.shape


def test_does_not_mutate_input_array():
    img = _solid_bgr(1080, 1920)
    before = img.copy()

    overlay_and_encode_jpeg(img, cluster_index=2, cluster_size=4)

    assert np.array_equal(img, before), (
        "overlay_and_encode_jpeg must not mutate its ndarray argument; the "
        "activity may reuse the same RectifiedImage.data across operations."
    )


def test_overlay_writes_red_pixels_in_bottom_right():
    """The notebook draws cluster index text in the bottom-right corner in
    red (BGR 0,0,255). Sample that region and assert the red channel
    dominates somewhere there — the no-overlay baseline is mid-gray."""
    img = _solid_bgr(1080, 1920)

    out = overlay_and_encode_jpeg(img, cluster_index=1, cluster_size=5)
    decoded = _decode_jpeg(out)

    h, w = decoded.shape[:2]
    # Bottom-right window where the text origin (w-350, h-75) sits.
    region = decoded[h - 200 : h, w - 500 : w, :]
    # In BGR, "red dominates" means R(channel 2) >> B(0) and R >> G(1).
    red_dominant = (region[:, :, 2].astype(int) - region[:, :, 0].astype(int) > 50) & (
        region[:, :, 2].astype(int) - region[:, :, 1].astype(int) > 50
    )
    assert red_dominant.any(), (
        "Expected red text pixels in bottom-right region; none found. "
        "Either the overlay wasn't drawn, was drawn elsewhere, or used the wrong color."
    )


def test_different_indices_produce_different_outputs():
    """Sanity that the cluster_index/cluster_size args are actually
    rendered into the JPEG — '1/5' and '2/5' must differ pixel-wise."""
    img = _solid_bgr(1080, 1920)

    a = _decode_jpeg(overlay_and_encode_jpeg(img, cluster_index=1, cluster_size=5))
    b = _decode_jpeg(overlay_and_encode_jpeg(img, cluster_index=2, cluster_size=5))

    assert not np.array_equal(a, b), (
        "Outputs for cluster_index=1 vs 2 are identical — the index isn't "
        "being baked into the overlay text."
    )


def test_the_overlay_is_the_notebooks_byte_for_byte():
    """Pins v1's exact drawing -- text "i/N" at (w-350, h-75), HERSHEY_SIMPLEX
    scale 5, red, thickness 10, LINE_AA, default JPEG quality -- which v1's
    notebook-parity test held byte-for-byte (test_stage2_notebook_parity.py
    needs a real .ORF; this is its drawing half)."""
    img = _solid_bgr(1080, 1920)
    expected = img.copy()
    cv2.putText(
        expected,
        "3/7",
        (1920 - 350, 1080 - 75),
        cv2.FONT_HERSHEY_SIMPLEX,
        5,
        (0, 0, 255),
        10,
        cv2.LINE_AA,
    )
    ok, encoded = cv2.imencode(".jpg", expected)
    assert ok

    assert overlay_and_encode_jpeg(img, 3, 7) == encoded.tobytes()
