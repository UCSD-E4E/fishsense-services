"""Pure-logic tests for stage 9's slate-PDF render step.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_render_slate_pdf.py.

The data-worker receives the slate template as a PDF via the
file-exchange and must binarize it the same way the original notebook
did (pymupdf -> grayscale -> threshold@125 -> BGR). These tests use a
synthesized one-page PDF so they don't depend on a fixture file."""

from io import BytesIO

import numpy as np
import pymupdf

from fishsense_services_processor.slate_preprocess.activities import (
    render_slate_pdf_to_binarized_bgr,
)


def _make_pdf_bytes(width: float = 612.0, height: float = 792.0) -> bytes:
    """Render a tiny one-page PDF with a black square so we can assert
    on a known geometry after binarization."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    # Draw a filled black rectangle in the middle quarter.
    page.draw_rect(
        pymupdf.Rect(width * 0.25, height * 0.25, width * 0.75, height * 0.75),
        color=(0, 0, 0),
        fill=(0, 0, 0),
    )
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def test_returns_3channel_uint8_bgr():
    pdf = _make_pdf_bytes()
    out = render_slate_pdf_to_binarized_bgr(pdf, dpi=72)
    assert out.dtype == np.uint8
    assert out.ndim == 3 and out.shape[2] == 3


def test_only_two_distinct_pixel_values_after_threshold():
    """Binarization must produce only 0 and 255."""
    pdf = _make_pdf_bytes()
    out = render_slate_pdf_to_binarized_bgr(pdf, dpi=72)
    unique_values = set(np.unique(out).tolist())
    assert unique_values <= {0, 255}, f"got {unique_values}"


def test_dpi_scales_resolution():
    pdf = _make_pdf_bytes(width=100.0, height=50.0)
    low = render_slate_pdf_to_binarized_bgr(pdf, dpi=72)
    high = render_slate_pdf_to_binarized_bgr(pdf, dpi=144)
    # Doubling DPI doubles each side (with 1-px tolerance on rounding).
    assert abs(high.shape[0] - 2 * low.shape[0]) <= 2
    assert abs(high.shape[1] - 2 * low.shape[1]) <= 2


def test_black_region_renders_as_zero_pixels():
    """The middle-quarter black rectangle should be all-zeros after
    threshold (it's well below the 125 threshold)."""
    pdf = _make_pdf_bytes()
    out = render_slate_pdf_to_binarized_bgr(pdf, dpi=72)
    h, w = out.shape[:2]
    middle = out[h // 2, w // 2]
    assert (middle == 0).all(), f"middle pixel not black: {middle}"
