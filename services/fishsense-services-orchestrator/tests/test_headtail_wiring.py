"""The head/tail chain across the orchestrator and the processor.

The orchestrator's real parents and activities and the processor's real
workflows and activities, on their real queues, with fakes only at the edges
(the catalog, S3, the model): stubbed activities cannot catch a payload that
does not round-trip between the two packages, or a name one calls that the
other doesn't register -- these do. (The same idea as test_worker.py's stage 1
test.) The NRP wakes are the real activities, unconfigured: no-ops, as in
compose and e2e.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_api.headtail_store import (
    HeadtailCandidate,
    HeadtailPreprocessInputs,
    LaserDot,
    PredictCapture,
    PredictionCandidate,
    PreprocessCapture,
)
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.headtail.activities import (
    HeadtailActivities,
    HeadtailTarget,
)
from fishsense_services_orchestrator.headtail.workflow import (
    PredictHeadtailImagesParentWorkflow,
    PreprocessHeadtailImagesParentWorkflow,
)
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import OrchestratorObjectStore
from fishsense_services_orchestrator.worker import build_worker
from fishsense_services_processor.headtail_predict import (
    activities as predict_activities,
)
from fishsense_services_processor.headtail_predict.activities import (
    HeadtailPredictActivities,
)
from fishsense_services_processor.headtail_predict.workflow import (
    PredictHeadtailImagesWorkflow,
)
from fishsense_services_processor.headtail_preprocess import (
    activities as preprocess_activities,
)
from fishsense_services_processor.headtail_preprocess.activities import (
    HeadtailPreprocessActivities,
)
from fishsense_services_processor.headtail_preprocess.workflow import (
    PreprocessHeadtailImagesWorkflow,
)
from fishsense_services_processor.registry import ROLE_GPU, ROLE_PER_IMAGE, Stage
from fishsense_services_processor.worker import build_worker as build_processor

from temporalio import activity

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
T0 = datetime(2026, 9, 1, tzinfo=UTC)
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]
LAYOUT = ObjectLayout(
    ObjectStoreConnection(
        endpoint_url="https://s3.example.test", region="garage", access_key_id="k",
        secret_access_key="s", bucket="fishsense-lite",
        labels_bucket="labels-fishsense-lite", legacy_labels_prefix="fishsense-lite",
    )  # fmt: skip
)


def _activities_of(instance) -> list:
    return [
        getattr(instance, name)
        for name in dir(instance)
        if hasattr(getattr(instance, name), "__temporal_activity_definition")
    ]


class _Catalog:
    def __init__(self, laser):
        self.laser = laser
        self.capture = uuid.uuid4()
        self.persisted = []
        self.cleared = []

    async def member_tenants(self):
        return [TENANT]

    async def next_dive_for_headtail_preprocessing(self, tenant_id):
        return HeadtailCandidate(DIVE, T0)

    async def headtail_preprocess_inputs(self, tenant_id, dive_id):
        return HeadtailPreprocessInputs(
            [PreprocessCapture(self.capture, "a" * 32, from_v1=False)], K, D
        )

    async def clear_headtail_needs_reprocess(self, tenant_id, dive_id, checksums=None):
        self.cleared.append(checksums)
        return 1

    async def next_dive_for_headtail_prediction(self, tenant_id, *, predictor_version):
        return PredictionCandidate(DIVE, T0, never_predicted=True)

    async def headtail_predict_captures(self, tenant_id, dive_id, *, predictor_version):
        return [
            PredictCapture(
                capture_id=self.capture, checksum="a" * 32, from_v1=False,
                dots=(LaserDot(self.laser, 2000.0, 1500.0),),
                has_existing_prediction=False, existing_laser_superseded=False,
            )
        ]  # fmt: skip

    async def persist_headtail_predictions(self, tenant_id, dive_id, rows):
        self.persisted.extend(rows)
        return len(rows)


class _S3:
    """Every HEAD finds its object (the JPEG was rendered)."""

    def head_object(self, Bucket, Key):  # pylint: disable=invalid-name
        return {}


class _ProcessorStore:
    def __init__(self, jpeg=b""):
        self.jpeg = jpeg
        self.written = []

    async def download_raw(self, ref, directory):
        path = Path(directory) / "raw.ORF"
        path.write_bytes(b"raw")
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.written.append((ref, data))

    async def download_processed_jpeg(self, ref):
        return self.jpeg


@activity.defn(name="stage_raw_bytes_for_dive")
async def _stage(target: StagingTarget) -> StageRawBytesResult:
    return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)


@activity.defn(name="cleanup_raw_bytes_for_dive")
async def _cleanup(target: StagingTarget) -> CleanupRawBytesResult:
    return CleanupRawBytesResult(deleted=1)


def _orchestrator(env, catalog):
    return build_worker(
        env.client,
        activities=[
            *_activities_of(
                HeadtailActivities(
                    catalog=catalog, store=OrchestratorObjectStore(_S3(), LAYOUT)
                )
            ),
            *_activities_of(NrpActivities(config=None)),
            _stage,
            _cleanup,
        ],
        task_queue="wiring",
    )


async def test_stage_5_1_runs_across_the_orchestrator_and_the_processor(monkeypatch):
    monkeypatch.setattr(
        preprocess_activities, "rectify_and_encode_jpeg", lambda *a: b"\xff\xd8jpeg"
    )
    catalog = _Catalog(uuid.uuid4())
    store = _ProcessorStore()
    processor = HeadtailPreprocessActivities(store_factory=lambda: store)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            _orchestrator(env, catalog),
            build_processor(
                env.client,
                role=ROLE_PER_IMAGE,
                stages=[
                    Stage(
                        "headtail_preprocess",
                        ROLE_PER_IMAGE,
                        [PreprocessHeadtailImagesWorkflow],
                        [processor.preprocess_headtail_image],
                    )
                ],
            ),
        ):
            target = await env.client.execute_workflow(
                PreprocessHeadtailImagesParentWorkflow.run,
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert target == HeadtailTarget(TENANT, DIVE)
    assert store.written == [
        (LAYOUT.processed_jpeg(TENANT, "preprocess_headtail_jpeg", "a" * 32),
         b"\xff\xd8jpeg")
    ]  # fmt: skip
    assert catalog.cleared == [["a" * 32]]


async def test_prediction_runs_across_the_orchestrator_and_the_processor(monkeypatch):
    """Without a GPU the processor runs the fallback; the mask it finds under
    the dot comes back as a persisted prediction naming that dot's label."""
    frame = np.full((3016, 4014, 3), 40, dtype=np.uint8)
    ok, jpeg = cv2.imencode(".jpg", frame)
    assert ok

    class _Segmentation:
        def inference(self, image):
            labels = np.zeros(image.shape[:2], dtype=np.int32)
            h, w = labels.shape
            cv2.ellipse(labels, (w // 2, h // 2), (200, 60), 0, 0, 360, 3, -1)
            return labels

    monkeypatch.setattr(predict_activities, "cuda_available", lambda: False)
    monkeypatch.setattr(predict_activities, "get_fallback_segmenter", _Segmentation)

    async def _no_sam3():
        raise AssertionError("no GPU: SAM 3.1's weights are never fetched")

    laser = uuid.uuid4()
    catalog = _Catalog(laser)
    processor = HeadtailPredictActivities(
        store_factory=lambda: _ProcessorStore(jpeg.tobytes()),
        sam3_checkpoint=_no_sam3,
    )
    backfilled = []

    @activity.defn(name="backfill_headtail_predictions_for_dive")
    async def backfill(target: HeadtailTarget) -> int:
        backfilled.append(target)
        return 0

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            build_worker(
                env.client,
                activities=[
                    *_activities_of(
                        HeadtailActivities(
                            catalog=catalog,
                            store=OrchestratorObjectStore(_S3(), LAYOUT),
                        )
                    ),
                    *_activities_of(NrpActivities(config=None)),
                    backfill,
                ],
                task_queue="wiring",
            ),
            build_processor(
                env.client,
                role=ROLE_GPU,
                stages=[
                    Stage(
                        "headtail_predict",
                        ROLE_GPU,
                        [PredictHeadtailImagesWorkflow],
                        [processor.predict_headtail_image],
                    )
                ],
            ),
        ):
            target = await env.client.execute_workflow(
                PredictHeadtailImagesParentWorkflow.run,
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert target == HeadtailTarget(TENANT, DIVE)
    (row,) = catalog.persisted
    assert (row.capture_id, row.status, row.laser_label_id) == (
        catalog.capture,
        "predicted",
        laser,
    )
    assert row.predictor_version == -1, "the fallback tier, so it stays stale"
    assert backfilled == [target]
    # v2 (contract 5): the kept mask's box reaches the row, around the dot --
    # what the species pre-annotation stage crops by.
    x_min, y_min, x_max, y_max = row.mask_bbox
    assert x_min < 2000 < x_max and y_min < 1500 < y_max
    assert (x_max - x_min, y_max - y_min) == (401, 121), "the 200x60 ellipse"
