"""Workflow contract test for PreprocessSlateImagesWorkflow, and its activity.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_preprocess_slate_images_workflow.py; names and
reasons are v1's.

v2 changes: each image arrives as a capture with the raw `ObjectRef` it was
staged to and the `ObjectRef` its composite goes to (v1: a checksum, from which
both workers built keys, plus an `output_folder`); the slate PDF is a ref too
(v1: a slate id). The processor reads and writes exactly those refs, which the
activity test at the end pins.
"""

import uuid
from datetime import timedelta
from typing import List, Optional, Tuple

import numpy as np
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    PreprocessSlateImage,
    PreprocessSlateImagesInput,
)
from fishsense_services_processor.slate_preprocess import activities as sut
from fishsense_services_processor.slate_preprocess.activities import (
    PreprocessSlateImageInput,
)
from fishsense_services_processor.slate_preprocess.workflow import (
    PreprocessSlateImagesWorkflow,
)

_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [-0.1, 0.05, 0.0, 0.0, 0.0]
_REF_POINTS = [(100.0, 200.0), (300.0, 400.0)]
_TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"
_SLATE = uuid.UUID(int=10)
_PDF = ObjectRef(bucket="scratch", key=f"tenants/{_TENANT}/slate_pdf/{_SLATE}.pdf")


def _image(name: str) -> PreprocessSlateImage:
    return PreprocessSlateImage(
        capture_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        raw=ObjectRef(bucket="scratch", key=f"tenants/{_TENANT}/raw/{name}.ORF"),
        jpeg=ObjectRef(
            bucket="labels",
            key=f"tenants/{_TENANT}/preprocess_slate_images_jpeg/{name}.JPG",
        ),
    )


def _input(names) -> PreprocessSlateImagesInput:
    return PreprocessSlateImagesInput(
        dive_id=uuid.UUID(int=383),
        slate_template_id=_SLATE,
        slate_pdf=_PDF,
        slate_dpi=300,
        reference_points=_REF_POINTS,
        camera_matrix=_K,
        distortion_coefficients=_D,
        images=[_image(name) for name in names],
    )


async def _run(names, stub, queue: str):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PreprocessSlateImagesWorkflow],
            activities=[stub],
        ):
            await env.client.execute_workflow(
                PreprocessSlateImagesWorkflow.run,
                _input(names),
                id=f"{queue}-{uuid.uuid4()}",
                task_queue=queue,
            )


async def test_workflow_fans_out_one_activity_per_image_with_correct_args():
    calls: List[PreprocessSlateImageInput] = []

    @activity.defn(name="preprocess_slate_image")
    async def stub(payload: PreprocessSlateImageInput) -> None:
        calls.append(payload)

    await _run(["a", "b"], stub, "test-stage9")

    assert {c.raw.key.rsplit("/", 1)[-1] for c in calls} == {"a.ORF", "b.ORF"}
    for c in calls:
        name = c.raw.key.rsplit("/", 1)[-1][:-4]
        assert c.jpeg == _image(name).jpeg
        assert c.slate_pdf == _PDF
        assert c.slate_dpi == 300
        assert [tuple(p) for p in c.reference_points] == _REF_POINTS
        assert c.camera_matrix == _K
        assert c.distortion_coefficients == _D


async def test_workflow_uses_start_to_close_not_schedule_to_close():
    """Same fan-out shape as head/tail's, same prod failure mode: a
    schedule-to-close counts the time a task waits behind the per-image cap,
    so a big dive's tail timed out without ever starting."""
    timeouts: List[Tuple[Optional[timedelta], Optional[timedelta]]] = []

    @activity.defn(name="preprocess_slate_image")
    async def stub(
        payload: PreprocessSlateImageInput,
    ) -> None:  # pylint: disable=unused-argument
        info = activity.info()
        timeouts.append((info.start_to_close_timeout, info.schedule_to_close_timeout))

    await _run(["a"], stub, "test-stage9-timeouts")

    assert len(timeouts) == 1
    start_to_close, schedule_to_close = timeouts[0]
    assert start_to_close == timedelta(minutes=5), "v1's per-image budget"
    if schedule_to_close is not None and schedule_to_close > timedelta(0):
        assert schedule_to_close > start_to_close


async def test_workflow_with_no_images_makes_no_activity_calls():
    calls: List[PreprocessSlateImageInput] = []

    @activity.defn(name="preprocess_slate_image")
    async def stub(payload: PreprocessSlateImageInput) -> None:
        calls.append(payload)

    await _run([], stub, "test-stage9-empty")

    assert not calls


class _FakeStore:
    def __init__(self) -> None:
        self.read: list[ObjectRef] = []
        self.written: list[tuple[ObjectRef, bytes]] = []

    async def download_raw(self, ref, directory):
        self.read.append(ref)
        path = directory / "frame.ORF"
        path.write_bytes(b"raw")
        return path

    async def download_slate_pdf(self, ref):
        self.read.append(ref)
        return b"%PDF"

    async def upload_processed_jpeg(self, ref, data):
        self.written.append((ref, data))


async def test_the_activity_reads_and_writes_exactly_the_refs_it_was_handed(
    monkeypatch,
):
    """v2: only the orchestrator issues keys (PLAN.md §9.11). The composite
    goes to the ref it was given -- over v1's JPEG for a migrated frame, so
    the URL a Label Studio task holds never moves."""
    store = _FakeStore()
    monkeypatch.setattr(sut, "open_store", lambda: store)
    built: list = []

    def fake_build(raw, pdf_bytes, camera_matrix, distortion, dpi, points):
        built.append((raw, pdf_bytes, dpi, points))
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(sut, "_build_slate_jpeg", fake_build)
    image = _image("a")
    payload = PreprocessSlateImageInput(
        capture_id=image.capture_id,
        raw=image.raw,
        jpeg=image.jpeg,
        slate_pdf=_PDF,
        slate_dpi=300,
        reference_points=_REF_POINTS,
        camera_matrix=_K,
        distortion_coefficients=_D,
    )

    await ActivityEnvironment().run(sut.preprocess_slate_image, payload)

    assert store.read == [image.raw, _PDF]
    assert store.written == [(image.jpeg, b"\xff\xd8jpeg")]
    _, pdf_bytes, dpi, points = built[0]
    assert (pdf_bytes, dpi, points) == (b"%PDF", 300, _REF_POINTS)


def test_the_composite_is_the_notebook_layout_end_to_end(monkeypatch):
    """The sync helper, with only the raw decode substituted: render the PDF,
    composite it left of the frame, encode a JPEG whose width is the scaled
    panel plus the photo."""
    from io import BytesIO

    import cv2
    import pymupdf

    frame = np.full((600, 800, 3), 200, np.uint8)

    class _FakeRectified:  # pylint: disable=too-few-public-methods
        def __init__(self, _raw, _intrinsics):
            self.data = frame

    monkeypatch.setattr(sut, "RectifiedImage", _FakeRectified)
    monkeypatch.setattr(sut, "RawImage", lambda raw: raw)

    doc = pymupdf.open()
    doc.new_page(width=300.0, height=600.0)
    buf = BytesIO()
    doc.save(buf)
    doc.close()

    jpeg = sut._build_slate_jpeg(  # pylint: disable=protected-access
        b"raw", buf.getvalue(), _K, _D, 72, [(10.0, 10.0)]
    )
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)

    assert decoded.shape[0] == 600
    assert decoded.shape[1] == 300 + 800  # a 1:2 panel scaled to 600 tall
