"""Pure-logic tests for stage 9's slate+image composite step.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_composite_slate_with_image.py.

Notebook layout: PDF rendering on the left, rectified image on the
right; reference points drawn as red filled circles + numbered labels,
each scaled by `img_height / pdf_height`."""

import numpy as np

from fishsense_services_processor.slate_preprocess.activities import (
    composite_slate_with_image,
)


def _synthetic_image(h: int, w: int, fill: int = 200) -> np.ndarray:
    return np.full((h, w, 3), fill, dtype=np.uint8)


def _synthetic_pdf_image(h: int, w: int) -> np.ndarray:
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    return img


def test_output_height_matches_image_height_after_scaling():
    """The slate is scaled so its height equals the image height. The
    final composite has that same height."""
    img = _synthetic_image(3000, 4000)
    pdf = _synthetic_pdf_image(800, 600)
    out = composite_slate_with_image(pdf, img, reference_points=[])
    assert out.shape[0] == img.shape[0]


def test_output_width_is_scaled_pdf_width_plus_image_width():
    img = _synthetic_image(3000, 4000)
    pdf = _synthetic_pdf_image(800, 600)
    scale = img.shape[0] / pdf.shape[0]
    expected_pdf_width = int(pdf.shape[1] * scale)
    out = composite_slate_with_image(pdf, img, reference_points=[])
    assert out.shape[1] == expected_pdf_width + img.shape[1]


def test_image_is_placed_on_the_right_half():
    """The rectified image is composited on the right side; its content
    must survive untouched in that region."""
    img = _synthetic_image(3000, 4000, fill=200)
    pdf = _synthetic_pdf_image(800, 600)
    out = composite_slate_with_image(pdf, img, reference_points=[])
    pdf_width = out.shape[1] - img.shape[1]
    np.testing.assert_array_equal(out[:, pdf_width:, :], img)


def test_reference_point_draws_red_circle_at_scaled_location():
    img = _synthetic_image(3000, 4000)
    pdf = _synthetic_pdf_image(1500, 600)  # scale = 2
    rp = [(100.0, 200.0)]  # scales to (200, 400)
    out = composite_slate_with_image(pdf, img, reference_points=rp)

    # The red filled circle is radius 25 in BGR (0,0,255).
    # Sample the center pixel.
    cx, cy = 200, 400
    b, g, r = out[cy, cx]
    assert (b, g, r) == (0, 0, 255), f"center not red: {(b, g, r)}"


def test_does_not_mutate_inputs():
    img = _synthetic_image(2000, 3000)
    pdf = _synthetic_pdf_image(500, 400)
    img_orig = img.copy()
    pdf_orig = pdf.copy()
    composite_slate_with_image(pdf, img, reference_points=[(50.0, 100.0)])
    np.testing.assert_array_equal(img, img_orig)
    np.testing.assert_array_equal(pdf, pdf_orig)
