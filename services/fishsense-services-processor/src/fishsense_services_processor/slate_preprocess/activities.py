"""Stage 9: composite a binarized slate-template PDF render with the
rectified raw image, draw reference-point markers, and write the JPEG.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
preprocess_slate_image.py, and the per-image DTO from
workflows/preprocess_slate_images_workflow.py. The pure helpers
(`render_slate_pdf_to_binarized_bgr`, `composite_slate_with_image`) are v1's,
verbatim, and module-level so they are tested without the Temporal/S3
surface.

v2 changes: the processor is handed the raw frame's, the PDF's and the
composite's `ObjectRef`s and reads and writes exactly those (v1: keys built
from a checksum, a slate id and an output folder); the frame is decoded from
a scratch file rather than held in memory (`ProcessorObjectStore.download_raw`)
and rectified with fishsense-core's own `CameraIntrinsics` (v1: the SDK's).
"""

import asyncio
import tempfile
from pathlib import Path
from typing import List, Tuple
from uuid import UUID

import cv2
import numpy as np
import pymupdf
from fishsense_core.camera_intrinsics import CameraIntrinsics
from fishsense_core.image.raw_image import RawImage
from fishsense_core.image.rectified_image import RectifiedImage
from pydantic import BaseModel
from temporalio import activity

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_processor.object_store import ProcessorObjectStore

__all__ = [
    "PreprocessSlateImageInput",
    "composite_slate_with_image",
    "open_store",
    "preprocess_slate_image",
    "render_slate_pdf_to_binarized_bgr",
]

ReferencePoint = Tuple[float, float]


class PreprocessSlateImageInput(BaseModel):
    """Per-image payload passed to the preprocess_slate_image activity."""

    capture_id: UUID
    raw: ObjectRef
    jpeg: ObjectRef
    slate_pdf: ObjectRef
    slate_dpi: int
    reference_points: List[ReferencePoint]
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]


def open_store() -> ProcessorObjectStore:
    """The object store, from ``FISHSENSE_OBJECT_STORE_*`` (tests replace it)."""
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


def render_slate_pdf_to_binarized_bgr(pdf_bytes: bytes, dpi: int) -> np.ndarray:
    """Render page 0 of a slate template PDF at the given DPI, threshold
    at 125, and return a 3-channel BGR uint8 array — same shape the
    notebook produced via pymupdf -> grayscale -> threshold -> BGR."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        page: pymupdf.Page = document.load_page(0)
        pixmap: pymupdf.Pixmap = page.get_pixmap(dpi=dpi)
        raw = np.frombuffer(pixmap.samples, dtype=np.uint8)
        rgb = raw.reshape(pixmap.height, pixmap.width, pixmap.n)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        _, binarized = cv2.threshold(gray, 125, 255, cv2.THRESH_BINARY)
        return cv2.cvtColor(binarized, cv2.COLOR_GRAY2BGR)


def composite_slate_with_image(
    pdf_image: np.ndarray,
    rectified_image: np.ndarray,
    reference_points: List[ReferencePoint],
) -> np.ndarray:
    """Scale the slate render to match the rectified image height,
    horizontally concat (slate left, image right), and overlay each
    reference point as a red filled circle + numbered label.

    Coordinates in `reference_points` are in the original PDF pixel
    space and are scaled by `image_height / pdf_height` to land on the
    final canvas. Mirrors the original notebook's per-image cell
    exactly."""
    img_height, img_width = rectified_image.shape[:2]
    pdf_height, pdf_width = pdf_image.shape[:2]

    scale_y = float(img_height) / float(pdf_height)
    new_pdf_height = int(pdf_height * scale_y)
    new_pdf_width = int(pdf_width * scale_y)
    pdf_resized = cv2.resize(pdf_image, (new_pdf_width, new_pdf_height))

    canvas = np.zeros((img_height, img_width + new_pdf_width, 3), dtype=np.uint8)
    canvas[:, :new_pdf_width, :] = pdf_resized
    canvas[:, new_pdf_width:, :] = rectified_image

    for idx, (px, py) in enumerate(reference_points):
        x = int(px * scale_y)
        y = int(py * scale_y)
        cv2.circle(canvas, (x, y), radius=25, color=(0, 0, 255), thickness=-1)
        cv2.putText(
            canvas,
            f"{idx + 1}",
            (x + 20, y - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            5,
            (0, 0, 255),
            10,
            cv2.LINE_AA,
        )

    return canvas


def _build_slate_jpeg(
    raw,
    pdf_bytes: bytes,
    camera_matrix: list[list[float]],
    distortion_coefficients: list[float],
    slate_dpi: int,
    reference_points: List[ReferencePoint],
) -> bytes:
    """Sync helper run via asyncio.to_thread."""
    intrinsics = CameraIntrinsics(
        camera_matrix=np.array(camera_matrix, dtype=float),
        distortion_coefficients=np.array(distortion_coefficients, dtype=float),
    )
    rectified = RectifiedImage(RawImage(raw), intrinsics).data
    pdf_image = render_slate_pdf_to_binarized_bgr(pdf_bytes, dpi=slate_dpi)
    composite = composite_slate_with_image(pdf_image, rectified, reference_points)
    success, encoded = cv2.imencode(".jpg", composite)
    if not success:
        raise RuntimeError("cv2.imencode failed")
    return encoded.tobytes()


@activity.defn(name="preprocess_slate_image")
async def preprocess_slate_image(payload: PreprocessSlateImageInput) -> None:
    """Download one raw frame and the slate-template PDF, build the slate
    composite, and write the JPEG to the ref the orchestrator issued."""
    activity.logger.info(
        "preprocessing slate image capture=%s slate_pdf=%s",
        payload.capture_id,
        payload.slate_pdf.key,
    )

    store = open_store()
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = await store.download_raw(payload.raw, Path(tmpdir))
        pdf_bytes = await store.download_slate_pdf(payload.slate_pdf)
        jpeg_bytes = await asyncio.to_thread(
            _build_slate_jpeg,
            raw_path,
            pdf_bytes,
            payload.camera_matrix,
            payload.distortion_coefficients,
            payload.slate_dpi,
            [tuple(p) for p in payload.reference_points],
        )
    await store.upload_processed_jpeg(payload.jpeg, jpeg_bytes)
